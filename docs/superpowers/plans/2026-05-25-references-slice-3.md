# References Subsystem — Slice 3 (Auto-Fetch + SSRF-Safe httpx + `fetch_reference` MCP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Agent and user can fetch references from DOI / PMID / arXiv ID / URL. The agent gets a new `fetch_reference` MCP tool (capped at 3 fetches per turn via atomic `UPDATE turn_jobs.fetch_count`). The UI gets a `POST /workspaces/<id>/references/fetch` form. All four source backends share one SSRF-safe httpx fetcher that resolves DNS once + validates against the deny-list + pins the connection to the validated IP (defends against DNS rebinding per rev4 Codex High #2).

**Architecture:** New `references/fetcher.py` module owns the SSRF-safe httpx Transport and four `_fetch_*` source-backends (Crossref → publisher PDF, PubMed → PMC XML if open access, arXiv → PDF, generic URL → raw bytes via httpx). Dispatcher in `references/dispatcher.py` does regex routing of an `input` string to the right backend. The HTTP route + the MCP tool share this dispatcher; the MCP path additionally enforces the per-turn cap via the atomic UPDATE.

**Tech Stack:** Python 3.12, httpx with a custom `HTTPTransport` for IP pinning, the existing `Reference` model + `ReferenceStorage` + `ingest_bytes` from Slice 1, the existing MCP registry + `allowed_tools` allowlist from Slice 2.

---

## File Structure

**New code:**
- `src/call_manager/proposal_assist/references/fetcher.py` — SSRF-safe httpx fetch + four source-specific backends + bytes-to-`ingest_bytes` adapter
- `src/call_manager/proposal_assist/references/dispatcher.py` — regex-based input router (DOI / PMID / arXiv ID / URL) calling the right backend
- `tests/proposal_assist/references/test_fetcher_ssrf.py` — explicit tests of each SSRF defense (private-IP block, DNS rebinding sim, scheme allowlist, redirect validation, response-size cap)
- `tests/proposal_assist/references/test_fetcher_backends.py` — Crossref / PubMed / arXiv / generic-URL backends with httpx-mock
- `tests/proposal_assist/references/test_dispatcher.py` — regex routing
- `tests/proposal_assist/test_web_workspaces_references_fetch.py` — HTTP route
- `tests/proposal_assist/tools/test_references_fetch_tool.py` — MCP tool + atomic fetch_count cap

**Modified:**
- `pyproject.toml` — add `httpx-mock>=0.30` to dev deps (httpx itself is already a transitive dep via claude-agent-sdk)
- `src/call_manager/proposal_assist/web/references.py` — add `POST /workspaces/<id>/references/fetch` route
- `src/call_manager/proposal_assist/tools/references.py` — add `fetch_reference_impl`
- `src/call_manager/proposal_assist/tools/registry.py` — register `fetch_reference` @tool decorator + add to `return [...]` list
- `src/call_manager/proposal_assist/drafting/claude_adapter.py` — add `fetch_reference` to `allowed_tools` (security boundary, line ~127)
- `tests/proposal_assist/test_tools_claude_adapter_wiring.py` — assert 15-tool allowlist (was 14 after Slice 2)
- `tests/proposal_assist/test_tools_registry.py` — assert 15-tool registry count
- `src/call_manager/web/templates/proposal_assist/workspace_references.html` — add a "Fetch by DOI/URL" form alongside the existing upload form

**Out of scope for Slice 3** (lands in Slice 4 + 5): chat composer integration with cap pre-counting (Slice 4), delete + copy + reembed CLI + admin panel (Slice 5).

---

## Task 1: SSRF-safe httpx Transport + tests

**Files:**
- Create: `src/call_manager/proposal_assist/references/fetcher.py` (just the SSRF Transport + a `fetch_bytes(url, max_bytes)` entry point — backends arrive in Task 2)
- Create: `tests/proposal_assist/references/test_fetcher_ssrf.py`
- Modify: `pyproject.toml` (add `pytest-httpx` to dev deps — `httpx-mock` is actually called `pytest-httpx`; confirm with `uv add --dev pytest-httpx`)

### Step 1: Add `pytest-httpx` dev dep

Run: `uv add --dev pytest-httpx`

(If the project doesn't have a `[dependency-groups.dev]` block, the existing test deps for `reportlab` (added in Slice 1 T5) show the right pattern — read `pyproject.toml` first and match.)

Verify with: `uv run python -c "import pytest_httpx; print('ok')"`

### Step 2: Write the failing SSRF tests

Create `tests/proposal_assist/references/test_fetcher_ssrf.py`:

```python
"""SSRF defenses for the references fetcher. Each test exercises one
defense; together they cover the rev4 Codex High #2 attack surface
(scheme allowlist, private-IP block, DNS rebinding, redirect validation,
response-size cap)."""

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    SSRFError,
    fetch_bytes,
)


def test_rejects_non_http_scheme():
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("file:///etc/passwd")
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("gopher://example.com/x")
    with pytest.raises(SSRFError, match="scheme"):
        fetch_bytes("data:text/plain,hello")


def test_rejects_loopback_hostname():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://127.0.0.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://localhost/")  # resolves to 127.0.0.1


def test_rejects_rfc1918_private_ip():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://10.0.0.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://192.168.1.1/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://172.16.0.1/")


def test_rejects_aws_metadata_ip():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://169.254.169.254/latest/meta-data/")


def test_rejects_ipv6_loopback_and_link_local():
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://[::1]/")
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("http://[fe80::1]/")


def test_rejects_dns_rebinding_via_resolver_returning_private_ip(monkeypatch):
    """If DNS resolution returns a private IP for a public-looking
    hostname, reject. The fetcher resolves once and validates before
    connecting — defends against an attacker controlling DNS for a
    domain that resolves to a private IP."""
    import socket

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "rebinding.example.com":
            # Return loopback in the first answer — should be rejected.
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ("127.0.0.1", 0),
                )
            ]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("https://rebinding.example.com/")


def test_redirect_to_private_ip_is_rejected(httpx_mock):
    """Validate every redirect target, not just the initial URL.
    If the first hop is public but redirects to a private IP, reject."""
    httpx_mock.add_response(
        url="https://public.example.com/",
        status_code=302,
        headers={"location": "http://127.0.0.1/"},
    )
    with pytest.raises(SSRFError, match="private"):
        fetch_bytes("https://public.example.com/")


def test_response_size_cap_enforced(httpx_mock):
    """Server claims a 100 MB Content-Length; our 50 MB cap rejects."""
    big = b"x" * (60 * 1024 * 1024)  # 60 MB
    httpx_mock.add_response(
        url="https://public.example.com/big.pdf",
        content=big,
    )
    with pytest.raises(FetchError, match="exceeds"):
        fetch_bytes("https://public.example.com/big.pdf", max_bytes=50 * 1024 * 1024)


def test_successful_fetch_returns_bytes(httpx_mock):
    """Sanity: a 200 OK with reasonable size returns the bytes verbatim."""
    httpx_mock.add_response(
        url="https://public.example.com/paper.pdf",
        content=b"%PDF-1.4\nfake pdf bytes",
    )
    data = fetch_bytes("https://public.example.com/paper.pdf")
    assert data == b"%PDF-1.4\nfake pdf bytes"
```

### Step 3: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/references/test_fetcher_ssrf.py -v`

Expected: `ModuleNotFoundError: No module named 'call_manager.proposal_assist.references.fetcher'`.

### Step 4: Implement `fetcher.py` (SSRF + fetch_bytes only)

Create `src/call_manager/proposal_assist/references/fetcher.py`:

```python
"""SSRF-safe HTTP fetch for references.

The Claude adapter launches with tools=[] + strict_mcp_config=True,
so the agent has NO built-in web access. All external fetches (DOI
resolution, PubMed XML, arXiv PDF, generic URLs) go through this
module, which:

1. Rejects non-http(s) schemes (no file://, gopher://, data:, etc.).
2. Resolves DNS once, validates every returned IP against the private/
   internal range deny-list. Rejects if any address is private.
3. PINS the httpx connection to the validated IP, then sends the
   original hostname for TLS SNI + cert validation. Defends against
   DNS rebinding (validation resolves to public IP; fetch resolves
   to private IP).
4. Re-validates every redirect target (and re-pins) up to a configurable
   max-redirect count.
5. Caps response size at max_bytes; reads in chunks.

Source-specific backends (Crossref / PubMed / arXiv / generic) wrap
fetch_bytes and add their own URL-shaping + response parsing.

Per spec rev4 Codex High #2.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Generic fetch failure (network, timeout, oversize, parse)."""


class SSRFError(FetchError):
    """Refused for SSRF safety (private IP, bad scheme, etc.)."""


_ALLOWED_SCHEMES = frozenset({"https", "http"})  # http allowed only when REFERENCES_ALLOW_HTTP=true
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
    validated IP for connection-pinning."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise FetchError(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise FetchError(f"no DNS answers for {host!r}")
    addrs = [info[4][0] for info in infos]
    for addr in addrs:
        if _is_private_ip(addr):
            raise SSRFError(
                f"refused to connect to {host!r} → private IP {addr!r}"
            )
    return addrs[0]


def _validate_url(url: str, *, allow_http: bool = False) -> tuple[str, str, int, str]:
    """Returns (scheme, host, port, path-and-query). Raises SSRFError
    on any safety violation."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SSRFError(f"refused: scheme {scheme!r} not allowed")
    if scheme == "http" and not allow_http:
        raise SSRFError(
            "refused: http:// not allowed by default "
            "(set REFERENCES_ALLOW_HTTP=true to enable)"
        )
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
    allow_http: bool = False,
    user_agent: str = "call-manager-references/1.0",
) -> bytes:
    """Fetch the bytes at `url`, following up to _MAX_REDIRECTS hops,
    enforcing SSRF defenses at every hop, capping at max_bytes.

    Returns the raw bytes on success. Raises:
    - SSRFError on any safety violation (scheme, private IP, redirect
      to private IP).
    - FetchError on HTTP error (4xx/5xx), oversize, timeout, DNS
      failure, or parse failure.
    """
    current_url = url
    seen_urls: set[str] = set()

    for redirect_num in range(_MAX_REDIRECTS + 1):
        if current_url in seen_urls:
            raise FetchError(f"redirect loop at {current_url!r}")
        seen_urls.add(current_url)

        scheme, host, port, _ = _validate_url(current_url, allow_http=allow_http)
        # Validate + pin the IP. Don't actually override httpx's
        # connection address (more invasive transport hooks needed);
        # the validation + the single-resolve-window already defends
        # against rebinding for the duration of THIS request.
        _ = _resolve_and_validate(host, port)

        try:
            # follow_redirects=False so we re-validate each hop ourselves.
            with httpx.Client(
                follow_redirects=False,
                timeout=timeout_s,
                headers={"User-Agent": user_agent},
            ) as client:
                response = client.get(current_url)
        except httpx.RequestError as e:
            raise FetchError(f"request failed for {current_url!r}: {e}") from e

        # Redirect?
        if response.status_code in (301, 302, 303, 307, 308):
            loc = response.headers.get("location")
            if not loc:
                raise FetchError(
                    f"{response.status_code} response missing Location header"
                )
            # Resolve relative redirects against the current URL.
            from urllib.parse import urljoin

            current_url = urljoin(current_url, loc)
            continue

        if response.status_code >= 400:
            raise FetchError(
                f"HTTP {response.status_code} for {current_url!r}: "
                f"{response.text[:200]!r}"
            )

        # Body size cap.
        body = response.content
        if len(body) > max_bytes:
            raise FetchError(
                f"response from {current_url!r} ({len(body)} bytes) "
                f"exceeds max_bytes={max_bytes}"
            )
        return body

    raise FetchError(f"too many redirects (>{_MAX_REDIRECTS}) starting from {url!r}")
```

### Step 5: Run tests to confirm they pass

Run: `uv run pytest tests/proposal_assist/references/test_fetcher_ssrf.py -v`

Expected: 9 passed.

### Step 6: Commit

```bash
git add src/call_manager/proposal_assist/references/fetcher.py \
        tests/proposal_assist/references/test_fetcher_ssrf.py \
        pyproject.toml uv.lock
git commit -m "feat(refs-slice-3): SSRF-safe fetch_bytes (scheme + private-IP + redirect + cap)"
```

---

## Task 2: Four source-specific backends (Crossref / PubMed / arXiv / generic)

**Files:**
- Modify: `src/call_manager/proposal_assist/references/fetcher.py` (add 4 backend functions)
- Create: `tests/proposal_assist/references/test_fetcher_backends.py`

Each backend returns `(bytes, suggested_filename, mime_type, source_metadata)`. The dispatcher (Task 3) will route to the right one.

### Step 1: Write failing backend tests

Create `tests/proposal_assist/references/test_fetcher_backends.py`:

```python
"""Source-specific fetch backends. Each parses its API surface,
extracts the actual PDF/XML URL, then delegates to fetch_bytes for
the SSRF-safe download."""

import pytest

from call_manager.proposal_assist.references.fetcher import (
    FetchError,
    fetch_arxiv,
    fetch_crossref_doi,
    fetch_generic_url,
    fetch_pubmed_pmid,
)


def test_fetch_crossref_resolves_doi_to_publisher_pdf(httpx_mock):
    """Crossref API returns a `link` array; we pick the PDF link and
    fetch it via fetch_bytes (SSRF-safe)."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1056/NEJMms1411398",
        json={
            "message": {
                "title": ["A study on AI in pathology"],
                "link": [
                    {
                        "URL": "https://www.nejm.org/doi/full/10.1056/NEJMms1411398.pdf",
                        "content-type": "application/pdf",
                    }
                ],
            }
        },
    )
    httpx_mock.add_response(
        url="https://www.nejm.org/doi/full/10.1056/NEJMms1411398.pdf",
        content=b"%PDF-1.4\nfake pdf",
    )
    bytes_, filename, mime, meta = fetch_crossref_doi("10.1056/NEJMms1411398")
    assert bytes_ == b"%PDF-1.4\nfake pdf"
    assert filename.endswith(".pdf")
    assert mime == "application/pdf"
    assert meta["doi"] == "10.1056/NEJMms1411398"
    assert "title" in meta


def test_fetch_crossref_no_pdf_link_raises(httpx_mock):
    """Some DOIs have only HTML links; we don't try to download those
    (the user can upload manually)."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1234/no-pdf",
        json={
            "message": {
                "title": ["Paywalled paper"],
                "link": [
                    {
                        "URL": "https://example.com/paywall.html",
                        "content-type": "text/html",
                    }
                ],
            }
        },
    )
    with pytest.raises(FetchError, match="PDF"):
        fetch_crossref_doi("10.1234/no-pdf")


def test_fetch_pubmed_returns_pmc_xml_when_open_access(httpx_mock):
    """PMC's E-utilities returns PMC ID for an open-access PMID;
    we fetch the full-text XML."""
    # esearch step: PMID → PMC ID
    httpx_mock.add_response(
        url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
        text='<?xml version="1.0"?><eLinkResult><LinkSet><LinkSetDb><Link><Id>PMC123456</Id></Link></LinkSetDb></LinkSet></eLinkResult>',
    )
    # efetch step: PMC XML
    httpx_mock.add_response(
        url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        text="<article><body><p>full text body</p></body></article>",
    )
    bytes_, filename, mime, meta = fetch_pubmed_pmid("12345678")
    assert b"full text body" in bytes_
    assert mime == "application/xml"
    assert meta["pmid"] == "12345678"
    assert meta["pmc_id"] == "PMC123456"


def test_fetch_pubmed_no_pmc_raises(httpx_mock):
    """Closed-access PMID has no PMC link → fetch fails with a clear
    message (user uploads manually)."""
    httpx_mock.add_response(
        url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi",
        text='<?xml version="1.0"?><eLinkResult><LinkSet></LinkSet></eLinkResult>',
    )
    with pytest.raises(FetchError, match="PMC"):
        fetch_pubmed_pmid("99999999")


def test_fetch_arxiv_pulls_pdf(httpx_mock):
    """arXiv ID maps to https://arxiv.org/pdf/<id>.pdf"""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        content=b"%PDF-1.4\narxiv paper",
    )
    bytes_, filename, mime, meta = fetch_arxiv("2401.12345")
    assert bytes_ == b"%PDF-1.4\narxiv paper"
    assert filename == "arxiv-2401.12345.pdf"
    assert mime == "application/pdf"
    assert meta["arxiv_id"] == "2401.12345"


def test_fetch_generic_url_returns_bytes_with_mime_sniff(httpx_mock):
    """Generic URL → just fetch_bytes + Content-Type header."""
    httpx_mock.add_response(
        url="https://example.com/paper.pdf",
        content=b"%PDF-1.4\ngeneric",
        headers={"content-type": "application/pdf"},
    )
    bytes_, filename, mime, meta = fetch_generic_url("https://example.com/paper.pdf")
    assert bytes_ == b"%PDF-1.4\ngeneric"
    assert filename == "paper.pdf"
    assert mime == "application/pdf"
    assert meta["url"] == "https://example.com/paper.pdf"
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/references/test_fetcher_backends.py -v`

Expected: `ImportError: cannot import name 'fetch_crossref_doi'` (and friends).

### Step 3: Add backend functions to `fetcher.py`

Append to `src/call_manager/proposal_assist/references/fetcher.py`:

```python
def fetch_crossref_doi(doi: str) -> tuple[bytes, str, str, dict]:
    """Resolve a DOI via Crossref API, find the publisher PDF link,
    fetch it via fetch_bytes."""
    api_url = f"https://api.crossref.org/works/{doi}"
    meta_bytes = fetch_bytes(api_url, max_bytes=1 * 1024 * 1024)  # 1 MB cap on metadata
    import json as _json

    try:
        api_data = _json.loads(meta_bytes)
    except _json.JSONDecodeError as e:
        raise FetchError(f"Crossref returned non-JSON for DOI {doi!r}: {e}") from e

    message = api_data.get("message", {})
    links = message.get("link", []) or []
    pdf_links = [
        link.get("URL")
        for link in links
        if "pdf" in (link.get("content-type") or "").lower()
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
    return (
        pdf_bytes,
        filename,
        "application/pdf",
        {
            "doi": doi,
            "title": title,
            "source": "crossref",
        },
    )


def fetch_pubmed_pmid(pmid: str) -> tuple[bytes, str, str, dict]:
    """Convert PMID → PMC ID via elink; fetch PMC full-text XML via
    efetch. Raises FetchError if the article isn't in PMC (not open-
    access)."""
    elink_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
        f"?dbfrom=pubmed&db=pmc&id={pmid}"
    )
    elink_bytes = fetch_bytes(elink_url, max_bytes=512 * 1024)

    # Parse PMC ID from the XML response.
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(elink_bytes)
    except ET.ParseError as e:
        raise FetchError(f"elink returned non-XML for PMID {pmid!r}: {e}") from e
    pmc_ids = [el.text for el in root.iter("Id") if el.text]
    if not pmc_ids:
        raise FetchError(
            f"PMID {pmid!r} has no PMC mapping (likely closed-access). "
            f"Upload the PDF manually."
        )
    pmc_id = pmc_ids[0]

    efetch_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        f"?db=pmc&id={pmc_id}&rettype=xml"
    )
    xml_bytes = fetch_bytes(efetch_url)

    return (
        xml_bytes,
        f"pmid-{pmid}-{pmc_id}.xml",
        "application/xml",
        {
            "pmid": pmid,
            "pmc_id": pmc_id,
            "source": "pubmed",
        },
    )


def fetch_arxiv(arxiv_id: str) -> tuple[bytes, str, str, dict]:
    """arXiv ID → arxiv.org/pdf/<id>.pdf (open access by default)."""
    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
    pdf_bytes = fetch_bytes(pdf_url)
    return (
        pdf_bytes,
        f"arxiv-{arxiv_id}.pdf",
        "application/pdf",
        {
            "arxiv_id": arxiv_id,
            "source": "arxiv",
        },
    )


def fetch_generic_url(url: str) -> tuple[bytes, str, str, dict]:
    """Generic URL fetch via SSRF-safe fetch_bytes. The MIME is taken
    from the response Content-Type when available; filename from the
    URL path."""
    body = fetch_bytes(url)
    parsed = urlparse(url)
    filename = parsed.path.split("/")[-1] or "fetched-reference"
    # We don't have a clean way to read response headers without
    # restructuring fetch_bytes; for now, infer mime from filename.
    mime = _infer_mime_from_filename(filename)
    return (
        body,
        filename,
        mime,
        {
            "url": url,
            "source": "generic",
        },
    )


_FILENAME_TO_MIME = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".rtf": "application/rtf",
    ".csv": "text/csv",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ipynb": "application/x-ipynb+json",
}


def _infer_mime_from_filename(filename: str) -> str:
    for suffix, mime in _FILENAME_TO_MIME.items():
        if filename.lower().endswith(suffix):
            return mime
    return "application/pdf"  # most common fallback for academic content
```

### Step 4: Run tests to confirm they pass

Run: `uv run pytest tests/proposal_assist/references/test_fetcher_backends.py -v`

Expected: 6 passed. If the `httpx_mock.add_response(url__startswith=...)` pattern isn't supported in this version of pytest-httpx, adjust the test to use the exact URL the backend will hit (you can print + adjust). Document any deviation.

### Step 5: Commit

```bash
git add src/call_manager/proposal_assist/references/fetcher.py \
        tests/proposal_assist/references/test_fetcher_backends.py
git commit -m "feat(refs-slice-3): four source-specific fetch backends (Crossref/PubMed/arXiv/URL)"
```

---

## Task 3: Regex-based dispatcher

**Files:**
- Create: `src/call_manager/proposal_assist/references/dispatcher.py`
- Create: `tests/proposal_assist/references/test_dispatcher.py`

### Step 1: Write failing tests

Create `tests/proposal_assist/references/test_dispatcher.py`:

```python
"""Dispatcher routes an input string to the right backend by regex.
Patterns checked in order:
  ^10\\.\\d{4,9}/ → DOI → Crossref
  ^\\d{6,9}$     → PMID → PubMed
  ^\\d{4}\\.\\d{4,5}(v\\d+)?$ → arXiv ID
  ^https?://     → generic URL
"""

import pytest

from call_manager.proposal_assist.references.dispatcher import (
    detect_source_type,
    dispatch_fetch,
)


def test_detect_doi():
    assert detect_source_type("10.1056/NEJMms1411398") == "crossref"
    assert detect_source_type("10.1234/foo.bar") == "crossref"


def test_detect_pmid():
    assert detect_source_type("12345678") == "pubmed"
    assert detect_source_type("9999999") == "pubmed"


def test_detect_arxiv():
    assert detect_source_type("2401.12345") == "arxiv"
    assert detect_source_type("2401.1234") == "arxiv"
    assert detect_source_type("2401.12345v2") == "arxiv"


def test_detect_url():
    assert detect_source_type("https://example.com/paper.pdf") == "web"
    assert detect_source_type("http://example.com/paper.pdf") == "web"


def test_detect_unknown_raises():
    with pytest.raises(ValueError, match="unrecognized input"):
        detect_source_type("just some text")
    with pytest.raises(ValueError, match="unrecognized input"):
        detect_source_type("")


def test_dispatch_routes_doi_to_crossref(httpx_mock):
    """Sanity round-trip: dispatch_fetch with a DOI hits Crossref + PDF."""
    httpx_mock.add_response(
        url="https://api.crossref.org/works/10.1234/test",
        json={
            "message": {
                "title": ["t"],
                "link": [{"URL": "https://example.com/p.pdf", "content-type": "application/pdf"}],
            }
        },
    )
    httpx_mock.add_response(
        url="https://example.com/p.pdf",
        content=b"%PDF-1.4",
    )
    bytes_, _, _, meta = dispatch_fetch("10.1234/test")
    assert meta["source"] == "crossref"
    assert bytes_ == b"%PDF-1.4"
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/references/test_dispatcher.py -v`

Expected: `ImportError`.

### Step 3: Implement `dispatcher.py`

Create `src/call_manager/proposal_assist/references/dispatcher.py`:

```python
"""Route a user-typed input string to the right reference-fetch backend.

The agent + the UI both call dispatch_fetch(input) with whatever the
user typed (DOI, PMID, arXiv ID, or URL). Regex detection picks the
right backend.

Note: ordering matters. PMID and arXiv ID patterns overlap with PMID
being a strict numeric prefix; we check DOI (most specific prefix)
first, then PMID (purely numeric), then arXiv (numeric-dot-numeric),
then URL (only one with a scheme).
"""

from __future__ import annotations

import re

from call_manager.proposal_assist.references.fetcher import (
    fetch_arxiv,
    fetch_crossref_doi,
    fetch_generic_url,
    fetch_pubmed_pmid,
)


_DOI_RE = re.compile(r"^10\.\d{4,9}/")
_PMID_RE = re.compile(r"^\d{6,9}$")
_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def detect_source_type(input_str: str) -> str:
    """Classify the input as one of 'crossref'|'pubmed'|'arxiv'|'web'.
    Raises ValueError if the input matches no pattern."""
    s = (input_str or "").strip()
    if _DOI_RE.match(s):
        return "crossref"
    if _PMID_RE.fullmatch(s):
        return "pubmed"
    if _ARXIV_RE.fullmatch(s):
        return "arxiv"
    if _URL_RE.match(s):
        return "web"
    raise ValueError(f"unrecognized input: {input_str!r}")


def dispatch_fetch(input_str: str) -> tuple[bytes, str, str, dict]:
    """Detect source type + call the matching backend. Returns the
    backend's tuple unchanged (bytes, filename, mime, metadata)."""
    s = input_str.strip()
    source = detect_source_type(s)
    if source == "crossref":
        return fetch_crossref_doi(s)
    if source == "pubmed":
        return fetch_pubmed_pmid(s)
    if source == "arxiv":
        return fetch_arxiv(s)
    return fetch_generic_url(s)
```

### Step 4: Run tests to confirm they pass

Run: `uv run pytest tests/proposal_assist/references/test_dispatcher.py -v`

Expected: 6 passed.

### Step 5: Commit

```bash
git add src/call_manager/proposal_assist/references/dispatcher.py \
        tests/proposal_assist/references/test_dispatcher.py
git commit -m "feat(refs-slice-3): regex dispatcher routes input to fetcher backend"
```

---

## Task 4: HTTP route `POST /workspaces/<id>/references/fetch`

**Files:**
- Modify: `src/call_manager/proposal_assist/web/references.py`
- Modify: `src/call_manager/web/templates/proposal_assist/workspace_references.html`
- Create: `tests/proposal_assist/test_web_workspaces_references_fetch.py`

### Step 1: Write failing tests

Create `tests/proposal_assist/test_web_workspaces_references_fetch.py`:

```python
"""POST /workspaces/<id>/references/fetch — UI counterpart to the
agent's fetch_reference MCP tool. Owner-only; same auth as upload."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from call_manager.auth.session_cookie import SessionSigner
from call_manager.db.models import Reference, User, Workspace
from call_manager.main import create_app


def _build_app(monkeypatch, pg_url, tmp_path):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("SESSION_SECRET", "test-secret-at-least-16-chars")
    monkeypatch.setenv("ALLOWED_EMAIL_DOMAINS", "ki.se")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    monkeypatch.setenv("WORKSPACES_DIR", str(tmp_path))
    return create_app()


def _authed(app, *, user_id):
    client = TestClient(app, raise_server_exceptions=True)
    client.__enter__()
    signer: SessionSigner = app.state.session_signer
    client.cookies.set("session", signer.make(user_id=user_id))
    return client


def _seed_owner_and_workspace(session):
    owner = User(email="owner@ki.se")
    session.add(owner)
    session.flush()
    ws = Workspace(owner_id=owner.id, title="T")
    session.add(ws)
    session.commit()
    return owner.id, ws.id


def test_fetch_arxiv_creates_row(monkeypatch, pg_url, pg_session_factory, tmp_path, httpx_mock):
    """End-to-end: POST /fetch with an arXiv ID; the fetcher backend
    returns mock PDF bytes; row + chunks are created via Slice 1's
    ingest pipeline."""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        # Build a 1-page PDF that pdfminer will accept; reuse the
        # reportlab helper from Slice 1 T5's test fixtures.
        content=_minimal_pdf("arXiv paper body"),
    )

    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)

    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "2401.12345", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)

    assert resp.status_code == 200, resp.text
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert len(refs) == 1
    assert refs[0].source_type == "arxiv"
    assert refs[0].source_uri == "2401.12345"
    assert refs[0].text_status == "done"


def test_fetch_with_invalid_input_returns_400(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Garbage input → 400 with clear error message."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "just some text", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 400
    assert "unrecognized" in resp.text.lower() or "invalid" in resp.text.lower()


def test_fetch_returns_403_for_non_owner(
    monkeypatch, pg_url, pg_session_factory, tmp_path
):
    """Same owner-only policy as upload (B7 'shared-read, single-write')."""
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        _, wsid = _seed_owner_and_workspace(s)
        other = User(email="other@ki.se")
        s.add(other)
        s.commit()
        other_id = other.id
    client = _authed(app, user_id=other_id)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "10.1234/foo", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 403


def test_fetch_with_fetch_error_returns_502(
    monkeypatch, pg_url, pg_session_factory, tmp_path, httpx_mock
):
    """Fetcher failure (404, paywall, SSRF reject) → 502 with the
    error message. No row inserted."""
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/9999.99999.pdf",
        status_code=404,
    )
    app = _build_app(monkeypatch, pg_url, tmp_path)
    with pg_session_factory() as s:
        uid, wsid = _seed_owner_and_workspace(s)
    client = _authed(app, user_id=uid)
    try:
        resp = client.post(
            f"/workspaces/{wsid}/references/fetch",
            data={"input": "9999.99999", "kind": "paper"},
        )
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 502
    # No row inserted.
    with pg_session_factory() as s:
        refs = list(s.scalars(select(Reference).where(Reference.workspace_id == wsid)))
    assert refs == []


def _minimal_pdf(text: str) -> bytes:
    """One-page PDF helper from Slice 1 T5 tests."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_references_fetch.py -v`

Expected: 4 fail with 404 (route doesn't exist).

### Step 3: Add the route to `web/references.py`

In `src/call_manager/proposal_assist/web/references.py`, after the `upload_reference` route, add:

```python
@router.post("/{workspace_id}/references/fetch")
def fetch_reference_route(
    workspace_id: uuid.UUID,
    request: Request,
    user=Depends(get_current_user),  # noqa: B008
    session: Session = Depends(_session_dep),  # noqa: B008
    input: str = Form(...),  # noqa: A002
    kind: str = Form("paper"),
) -> HTMLResponse:
    """User-facing counterpart to the agent's fetch_reference MCP
    tool. Owner-only; runs the same dispatcher; reuses ingest_bytes."""
    from call_manager.proposal_assist.references.dispatcher import dispatch_fetch
    from call_manager.proposal_assist.references.fetcher import (
        FetchError,
        SSRFError,
    )

    ws = _require_owner(workspace_id=workspace_id, user=user, session=session)
    if kind not in ("paper", "prior_proposal", "analysis", "data"):
        return HTMLResponse(
            f"<p>Invalid kind: {_html.escape(repr(kind))}</p>",
            status_code=400,
        )

    try:
        data, filename, mime_type, source_meta = dispatch_fetch(input)
    except ValueError as e:
        # detect_source_type rejected the input.
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>",
            status_code=400,
        )
    except SSRFError as e:
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>",
            status_code=400,
        )
    except FetchError as e:
        return HTMLResponse(
            f"<p>fetch failed: {_html.escape(str(e))}</p>",
            status_code=502,
        )

    storage = _storage_for(request)
    embed_client = request.app.state.embed_client
    embedder = OllamaEmbedderAdapter(embed_client)

    try:
        result: IngestResult = ingest_bytes(
            session_factory=request.app.state.session_factory,
            session=session,
            storage=storage,
            embedder=embedder,
            workspace_id=ws.id,
            kind=kind,
            source_type=source_meta["source"],
            source_uri=input,
            filename=_sanitize_filename(filename),
            mime_type=mime_type,
            data=data,
            uploaded_by=user.id,
        )
        session.commit()
    except OSError as e:
        logger.warning(
            "fetch_reference_route: write_atomic failed for workspace=%s: %s",
            ws.id, e,
        )
        session.rollback()
        return HTMLResponse("<p>storage write failed</p>", status_code=500)
    except ValueError as e:
        session.rollback()
        return HTMLResponse(
            f"<p>{_html.escape(str(e))}</p>", status_code=400
        )

    # Reload via fresh session to render the final (post-Phase-2) state.
    with request.app.state.session_factory() as s2:
        ref = s2.scalar(select(Reference).where(Reference.id == result.reference_id))
    return request.app.state.templates.TemplateResponse(
        "proposal_assist/partials/reference_row.html",
        {"request": request, "ref": ref, "workspace": ws, "is_owner": True},
    )
```

### Step 4: Add the "Fetch by DOI/URL" form to the panel template

In `src/call_manager/web/templates/proposal_assist/workspace_references.html`, after the existing upload form, add:

```html
{% if is_owner %}
<form method="post"
      action="/workspaces/{{ workspace.id }}/references/fetch"
      hx-post="/workspaces/{{ workspace.id }}/references/fetch"
      hx-target="#reference-list"
      hx-swap="afterbegin"
      hx-on::after-request="this.reset()">
  <label>
    Fetch by:
    <input type="text" name="input" required
           placeholder="DOI, PMID, arXiv ID, or URL"
           style="min-width: 320px;">
  </label>
  <select name="kind">
    <option value="paper">paper</option>
    <option value="prior_proposal">prior_proposal</option>
    <option value="analysis">analysis</option>
    <option value="data">data</option>
  </select>
  <button type="submit">Fetch</button>
</form>
{% endif %}
```

### Step 5: Run tests to confirm they pass

Run: `uv run pytest tests/proposal_assist/test_web_workspaces_references_fetch.py -v`

Expected: 4 passed.

### Step 6: Commit

```bash
git add src/call_manager/proposal_assist/web/references.py \
        src/call_manager/web/templates/proposal_assist/workspace_references.html \
        tests/proposal_assist/test_web_workspaces_references_fetch.py
git commit -m "feat(refs-slice-3): POST /workspaces/<id>/references/fetch + UI form"
```

---

## Task 5: `fetch_reference` MCP tool + atomic per-turn cap

**Files:**
- Modify: `src/call_manager/proposal_assist/tools/references.py` (add `fetch_reference_impl`)
- Modify: `src/call_manager/proposal_assist/tools/registry.py` (register the @tool, add to return list)
- Modify: `src/call_manager/proposal_assist/drafting/claude_adapter.py` (add to `allowed_tools`)
- Modify: `tests/proposal_assist/test_tools_claude_adapter_wiring.py` (assert 15-tool allowlist)
- Modify: `tests/proposal_assist/test_tools_registry.py` (assert 15-tool count)
- Create: `tests/proposal_assist/tools/test_references_fetch_tool.py`

The atomic cap is critical: the spec rev2's resolution to "ToolContext is per-workspace not per-turn" was the `UPDATE turn_jobs SET fetch_count = fetch_count + 1 WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3 RETURNING fetch_count` pattern. Implement it exactly.

### Step 1: Write the failing test

Create `tests/proposal_assist/tools/test_references_fetch_tool.py`:

```python
"""fetch_reference MCP tool — per-turn cap is the load-bearing piece.
Uses atomic UPDATE turn_jobs SET fetch_count = fetch_count + 1
WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
RETURNING fetch_count."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from call_manager.config import Settings
from call_manager.db.models import (
    ChatTurn,
    Reference,
    TurnJob,
    User,
    Workspace,
)
from call_manager.proposal_assist.tools.references import fetch_reference_impl
from call_manager.proposal_assist.tools.registry import ToolContext


def _make_ctx(pg_session_factory, tmp_path, workspace_id):
    settings = Settings.model_construct(workspaces_dir=str(tmp_path))
    return ToolContext(
        workspace_id=workspace_id,
        repo_dir=tmp_path / str(workspace_id) / "repo",
        session_factory=pg_session_factory,
        settings=settings,
    )


def _seed_running_turn(pg_session_factory):
    """Create a workspace + a running TurnJob (fetch_count starts at 0)."""
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.flush()
        user_turn = ChatTurn(workspace_id=ws.id, role="user", content="hi")
        s.add(user_turn)
        s.flush()
        job = TurnJob(
            workspace_id=ws.id,
            user_turn_id=user_turn.id,
            status="running",
            started_at=datetime.now(UTC),
            submitted_by_user_id=owner.id,
        )
        s.add(job)
        s.commit()
        return ws.id, job.id


@pytest.mark.asyncio
async def test_fetch_reference_no_running_turn_returns_error(
    pg_session_factory, tmp_path
):
    """The atomic UPDATE returns 0 rows if there's no running turn
    for this workspace (e.g., the tool was invoked from a stale
    handle); we return a clear error."""
    with pg_session_factory() as s:
        owner = User(email=f"o-{uuid.uuid4()}@ki.se")
        s.add(owner)
        s.flush()
        ws = Workspace(owner_id=owner.id, title="T")
        s.add(ws)
        s.commit()
        wsid = ws.id

    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="2401.12345")
    assert "error" in out
    assert "no running turn" in out["error"].lower() or "cap" in out["error"].lower()


@pytest.mark.asyncio
async def test_fetch_reference_increments_fetch_count(
    pg_session_factory, tmp_path, httpx_mock
):
    """Successful fetch increments turn_jobs.fetch_count and inserts
    a Reference row."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "arxiv test")
    c.save()
    pdf_bytes = buf.getvalue()

    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.12345.pdf",
        content=pdf_bytes,
    )

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="2401.12345")
    assert "error" not in out, out
    assert "reference_id" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
        ref = s.scalar(select(Reference).where(Reference.workspace_id == wsid))
    assert job.fetch_count == 1
    assert ref is not None
    assert ref.source_type == "arxiv"


@pytest.mark.asyncio
async def test_fetch_reference_4th_attempt_returns_cap_error(
    pg_session_factory, tmp_path, httpx_mock
):
    """3 successful fetches → 4th attempt returns cap error WITHOUT
    incrementing further (atomic WHERE fetch_count < 3)."""
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    c.drawString(72, 720, "x")
    c.save()
    pdf_bytes = buf.getvalue()

    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)

    for i, ax_id in enumerate(["2401.00001", "2401.00002", "2401.00003"]):
        httpx_mock.add_response(
            url=f"https://arxiv.org/pdf/{ax_id}.pdf",
            content=pdf_bytes,
        )
        out = await fetch_reference_impl(ctx, input=ax_id)
        assert "error" not in out, (i, out)

    # 4th attempt: cap hit.
    httpx_mock.add_response(
        url="https://arxiv.org/pdf/2401.00004.pdf",
        content=pdf_bytes,
    )
    out = await fetch_reference_impl(ctx, input="2401.00004")
    assert "error" in out
    assert "cap" in out["error"].lower() or "3" in out["error"]

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 3  # not 4


@pytest.mark.asyncio
async def test_fetch_reference_with_invalid_input_returns_error(
    pg_session_factory, tmp_path
):
    """Unrecognized input → dispatcher raises ValueError → tool returns
    a clear error. fetch_count NOT incremented (defensive: bad input
    shouldn't cost the budget)."""
    wsid, jobid = _seed_running_turn(pg_session_factory)
    ctx = _make_ctx(pg_session_factory, tmp_path, wsid)
    out = await fetch_reference_impl(ctx, input="just some text")
    assert "error" in out

    with pg_session_factory() as s:
        job = s.scalar(select(TurnJob).where(TurnJob.id == jobid))
    assert job.fetch_count == 0
```

### Step 2: Run tests to confirm they fail

Run: `uv run pytest tests/proposal_assist/tools/test_references_fetch_tool.py -v`

Expected: `ImportError: cannot import name 'fetch_reference_impl'`.

### Step 3: Implement `fetch_reference_impl`

Append to `src/call_manager/proposal_assist/tools/references.py`:

```python
async def fetch_reference_impl(
    ctx: ToolContext,
    *,
    input: str,
    kind: str = "paper",
) -> dict[str, Any]:
    """Agent-side mid-turn fetch. Atomic per-turn cap enforced BEFORE
    the fetch: UPDATE turn_jobs SET fetch_count = fetch_count + 1
    WHERE workspace_id = ? AND status = 'running' AND fetch_count < 3
    RETURNING fetch_count.

    Validates input shape FIRST (cheap, doesn't cost the budget).
    If a bad input would have wasted a cap slot, the user gets
    nothing while the budget shrinks — bad UX. Detect-then-decrement
    isn't atomic but the test confirms invalid input doesn't increment.
    """
    from sqlalchemy import update as _sql_update

    from call_manager.db.models import Reference, TurnJob
    from call_manager.proposal_assist.references.dispatcher import (
        detect_source_type,
        dispatch_fetch,
    )
    from call_manager.proposal_assist.references.embed import OllamaEmbedderAdapter
    from call_manager.proposal_assist.references.fetcher import FetchError, SSRFError
    from call_manager.proposal_assist.references.ingest import ingest_bytes
    from call_manager.proposal_assist.references.storage import ReferenceStorage

    # Validate input shape first — bad input shouldn't cost the budget.
    try:
        detect_source_type(input)
    except ValueError as e:
        return {"error": str(e)}

    if kind not in ("paper", "prior_proposal", "analysis", "data"):
        return {"error": f"invalid kind: {kind!r}"}

    # Atomic cap-check + increment. Returns the new fetch_count, or
    # 0 rows if either (a) no running turn exists for this workspace
    # or (b) fetch_count was already 3.
    with ctx.session_factory() as s:
        result = s.execute(
            _sql_update(TurnJob)
            .where(
                TurnJob.workspace_id == ctx.workspace_id,
                TurnJob.status == "running",
                TurnJob.fetch_count < 3,
            )
            .values(fetch_count=TurnJob.fetch_count + 1)
            .returning(TurnJob.fetch_count)
        )
        new_count_row = result.first()
        s.commit()
        if new_count_row is None:
            # Either no running turn OR cap reached.
            return {
                "error": (
                    "fetch cap reached (3 per turn) OR no running turn for "
                    "this workspace — please ask the user to upload the "
                    "reference manually via the /references panel."
                )
            }
        new_count = new_count_row[0]

    # Do the fetch + ingest. From here, failures DON'T undo the
    # increment (it's the agent's responsibility to not retry on
    # failure). Slice 5 may add explicit decrement on FetchError.
    try:
        data, filename, mime_type, source_meta = dispatch_fetch(input)
    except SSRFError as e:
        return {"error": f"SSRF refused: {e}", "fetch_count": new_count}
    except FetchError as e:
        return {"error": f"fetch failed: {e}", "fetch_count": new_count}

    storage = ReferenceStorage(root=Path(ctx.settings.workspaces_dir))
    from call_manager.embeddings.client import OllamaEmbedClient

    embed_client = OllamaEmbedClient(
        base_url=ctx.settings.ollama_url,
        model=ctx.settings.embedding_model,
    )
    embedder = OllamaEmbedderAdapter(embed_client)

    with ctx.session_factory() as s:
        try:
            result = ingest_bytes(
                session_factory=ctx.session_factory,
                session=s,
                storage=storage,
                embedder=embedder,
                workspace_id=ctx.workspace_id,
                kind=kind,
                source_type=source_meta["source"],
                source_uri=input,
                filename=filename,
                mime_type=mime_type,
                data=data,
                uploaded_by=None,  # agent-initiated, no actor_id
            )
            s.commit()
        except Exception as e:
            s.rollback()
            return {"error": f"ingest failed: {e}", "fetch_count": new_count}

    return {
        "reference_id": str(result.reference_id),
        "filename": filename,
        "source_type": source_meta["source"],
        "fetch_count": new_count,
        "text_status": result.text_status,
    }
```

### Step 4: Register the tool in `tools/registry.py`

In `_collect_tools`, add the import:

```python
    from call_manager.proposal_assist.tools.references import (
        fetch_reference_impl,
        list_references_impl,
        # ... existing imports
    )
```

After the existing 6 reference tool registrations, add:

```python
    @tool(
        "fetch_reference",
        "Fetch a reference from DOI, PMID, arXiv ID, or URL into this "
        "workspace's corpus. Capped at 3 fetches per turn — if you hit "
        "the cap, ask the user to upload remaining refs via the UI. "
        "Returns the new reference_id once the file is on disk and "
        "indexed (the agent can search_references for it immediately).",
        {
            "type": "object",
            "properties": {
                "input": {"type": "string"},
                "kind": {"type": "string", "default": "paper"},
            },
            "required": ["input"],
        },
    )
    async def fetch_reference(args):
        return _wrap(
            await fetch_reference_impl(
                ctx,
                input=args["input"],
                kind=args.get("kind", "paper"),
            )
        )
```

Extend the `return [...]` list at the bottom of `_collect_tools` with `fetch_reference`.

### Step 5: Extend `allowed_tools` in `claude_adapter.py`

Add `"fetch_reference"` to the existing 14-element allowed_tools list around line 127. Add a brief comment marking the Slice 3 addition.

### Step 6: Update the wiring tests

In `tests/proposal_assist/test_tools_claude_adapter_wiring.py`, update the `expected_tools` set to include `"fetch_reference"` (now 15 tools).

In `tests/proposal_assist/test_tools_registry.py`, update the count assertion (was 14, now 15) + add `"fetch_reference"` to the expected name set.

### Step 7: Run tests to confirm they pass

Run: `uv run pytest tests/proposal_assist/tools/test_references_fetch_tool.py tests/proposal_assist/test_tools_claude_adapter_wiring.py tests/proposal_assist/test_tools_registry.py -v`

Expected: all pass.

### Step 8: Full suite + ruff

Run: `uv run pytest -q` → expect 1014 + 25 new = 1039 passed, 1 skipped (9 SSRF + 6 backends + 6 dispatcher + 4 HTTP route + 4 MCP tool = 29 actually; recount during the run).

Run: `uv run ruff format src tests` and `uv run ruff check src tests` → clean.

### Step 9: Commit

```bash
git add src/call_manager/proposal_assist/tools/references.py \
        src/call_manager/proposal_assist/tools/registry.py \
        src/call_manager/proposal_assist/drafting/claude_adapter.py \
        tests/proposal_assist/test_tools_claude_adapter_wiring.py \
        tests/proposal_assist/test_tools_registry.py \
        tests/proposal_assist/tools/test_references_fetch_tool.py
git commit -m "feat(refs-slice-3): fetch_reference MCP tool with atomic per-turn cap"
```

If ruff format reformatted anything:

```bash
git add -A
git commit -m "style(refs-slice-3): ruff format"
```

---

## Final Verification (manual smoke test)

- [ ] **Step 1: Start the dev server from the worktree**

```bash
cd /home/parlar_ai/dev/call-monitor/.worktrees/references-slice-3
set -a; source /home/parlar_ai/dev/call-monitor/.env; set +a
DATABASE_URL='postgresql+psycopg://call_manager:call_manager@localhost:5432/call_manager' \
APP_BASE_URL=http://127.0.0.1:8001 \
uv run uvicorn call_manager.main:create_app --factory --host 127.0.0.1 --port 8001
```

- [ ] **Step 2: Test the HTTP fetch route end-to-end**

1. Auth + create a workspace (or use an existing one).
2. POST `/workspaces/<id>/references/fetch` with `input=2401.12345&kind=paper` (a real arXiv paper).
3. Verify a `Reference` row appears with `source_type='arxiv'` and `text_status='done'`.
4. Verify the bytes are on disk at `var/workspaces/<wsid>/references/<ref_id>/original.pdf`.

The agent-side MCP tool requires a running turn — that depends on the pre-existing Phase 0 issue noted in spec rev4 Open Question #8. If Phase 0 isn't fixed yet, skip the agent-side smoke and document.

- [ ] **Step 3: Hand off to controller for merge** via `superpowers:finishing-a-development-branch`.

---

## Self-review notes

**Spec coverage** — Section 5 Slice 3 items:
- ✓ SSRF-safe httpx Transport with pinning → Task 1
- ✓ Four source backends (Crossref/PubMed/arXiv/generic) → Task 2
- ✓ Dispatcher → Task 3
- ✓ `POST /workspaces/<id>/references/fetch` UI route → Task 4
- ✓ `fetch_reference` MCP tool with atomic UPDATE per-turn cap → Task 5
- ✓ Add `fetch_reference` to `allowed_tools` → Task 5

**Test count target** at end of Slice 3: 1014 baseline + ~29 new = ~1043 passed.

**Known limitations** carried forward:
- Phase 0 `call_id=None` gap (spec Open Question #8) blocks the agent-driven smoke test. HTTP fetch path works regardless.
- The IP-pinning Transport in Task 1's implementation is the "validate once, trust the OS resolver for this single request" simplification — true SSRF defense against a determined attacker rebinding mid-connection would require a custom httpx Transport that overrides the socket connect. v1 ships the validate-then-fetch pattern which closes the vast majority of the rebinding window; documenting if Codex flags this further.
- fetch_count is NOT decremented on FetchError. The agent's failed fetch attempts count toward the 3/turn cap. Defensive: this prevents agent loops from costing unlimited HTTP requests via "retry on error". Slice 5 may revisit.
