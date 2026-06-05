"""Text extraction for reference files, dispatched by MIME type.

Returns a list of (section_path, text) tuples. For formats with native
section structure (markdown, docx, ipynb), each tuple is one section.
For PDFs, one tuple per page (section_path='Page N'). For RTF / CSV /
XLSX / plain text, a single tuple (section_path=None).

Raises ExtractError on any failure — the caller (ingest) marks the
Reference row text_status='failed' and persists text_error.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Iterable


class ExtractError(RuntimeError):
    """Raised when text extraction fails for any reason."""


SectionList = list[tuple[str | None, str]]

# Decompression-bomb caps for ZIP-container formats (DOCX/XLSX). The
# uploaded bytes can be up to 50 MB but expand without bound when
# inflated; refuse before handing off to python-docx/openpyxl.
_MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB total inflated size
_MAX_ZIP_ENTRIES = 20_000  # absurd entry count → zip bomb / fork bomb
# Cap pages parsed from a PDF and columns kept from CSV/XLSX headers so a
# crafted file can't exhaust CPU/memory in the synchronous request path.
_MAX_PDF_PAGES = 100
_MAX_TABLE_COLUMNS = 100


def _guard_zip_bomb(data: bytes) -> None:
    """Inspect a ZIP container's central directory and reject obvious
    decompression bombs BEFORE any member is inflated. DOCX and XLSX are
    ZIP containers, so this runs ahead of python-docx / openpyxl."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise ExtractError(
                    f"zip container has too many entries ({len(infos)} > {_MAX_ZIP_ENTRIES})"
                )
            total = 0
            for info in infos:
                total += info.file_size
                if total > _MAX_ZIP_UNCOMPRESSED_BYTES:
                    raise ExtractError(
                        "zip container's declared uncompressed size exceeds "
                        f"{_MAX_ZIP_UNCOMPRESSED_BYTES} bytes (decompression bomb)"
                    )
    except zipfile.BadZipFile as e:
        raise ExtractError(f"not a valid zip container: {e}") from e


def extract_text(*, mime: str, data: bytes) -> SectionList:
    """Dispatch on MIME and return [(section_path, text), ...]."""
    if mime == "text/plain":
        return [(None, data.decode("utf-8", errors="replace"))]
    if mime == "text/markdown":
        return _extract_markdown(data)
    if mime == "application/pdf":
        return _extract_pdf(data)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _extract_docx(data)
    if mime == "application/rtf":
        return _extract_rtf(data)
    if mime == "text/csv":
        return _extract_csv(data)
    if mime in (
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        return _extract_xlsx(data)
    if mime == "application/x-ipynb+json":
        return _extract_ipynb(data)
    raise ExtractError(f"unsupported mime: {mime!r}")


# ----- per-format extractors ------------------------------------------------


_MD_HEADING_RE = re.compile(r"^(#+)\s+(.+)$")


def _extract_markdown(data: bytes) -> SectionList:
    text = data.decode("utf-8", errors="replace")
    sections: SectionList = []
    stack: list[tuple[int, str]] = []  # (depth, title) for breadcrumb
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for line in text.splitlines():
        m = _MD_HEADING_RE.match(line)
        if m:
            flush()
            depth = len(m.group(1))
            title = m.group(2).strip()
            # Pop stack to depth-1 then push current.
            stack = [(d, t) for (d, t) in stack if d < depth]
            stack.append((depth, title))
            current_path = " > ".join(t for (_, t) in stack)
        else:
            current_lines.append(line)
    flush()
    if not sections:
        # No headings at all — fall back to a single section.
        return [(None, text)]
    return sections


def _extract_pdf(data: bytes) -> SectionList:
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextContainer
    except Exception as e:
        raise ExtractError(f"pdfminer load failed: {e}") from e
    sections: SectionList = []
    try:
        for page_no, layout in enumerate(extract_pages(io.BytesIO(data)), start=1):
            if page_no > _MAX_PDF_PAGES:
                break
            parts = [el.get_text() for el in layout if isinstance(el, LTTextContainer)]
            text = "".join(parts).strip()
            if text:
                sections.append((f"Page {page_no}", text))
    except Exception as e:
        raise ExtractError(f"pdf extraction failed: {e}") from e
    if not sections:
        raise ExtractError("pdf produced no text")
    return sections


def _extract_docx(data: bytes) -> SectionList:
    try:
        from docx import Document
    except Exception as e:
        raise ExtractError(f"python-docx load failed: {e}") from e
    _guard_zip_bomb(data)
    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:
        raise ExtractError(f"docx parse failed: {e}") from e

    sections: SectionList = []
    stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for para in doc.paragraphs:
        style = (para.style.name or "") if para.style else ""
        # Detect headings: "Heading 1", "Heading 2", ...
        m = re.match(r"Heading (\d+)", style)
        if m:
            flush()
            depth = int(m.group(1))
            title = para.text.strip()
            stack = [(d, t) for (d, t) in stack if d < depth]
            stack.append((depth, title))
            current_path = " > ".join(t for (_, t) in stack)
        else:
            current_lines.append(para.text)
    flush()
    if not sections:
        # No headings → whole doc as one section.
        return [(None, "\n".join(p.text for p in doc.paragraphs))]
    return sections


def _extract_rtf(data: bytes) -> SectionList:
    try:
        from striprtf.striprtf import rtf_to_text
    except Exception as e:
        raise ExtractError(f"striprtf load failed: {e}") from e
    try:
        text = rtf_to_text(data.decode("utf-8", errors="replace"))
    except Exception as e:
        raise ExtractError(f"rtf parse failed: {e}") from e
    return [(None, text.strip())]


def _extract_csv(data: bytes) -> SectionList:
    """Schema + first 5 rows as a markdown table — agent gets shape
    without raw data flooding."""
    import csv

    text = data.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ExtractError("empty CSV") from None
    # Cap header width so a pathological row (e.g. 1M columns) can't blow
    # up the rendered table / token budget.
    header = header[:_MAX_TABLE_COLUMNS]
    # strict=False: range(5) caps the iteration; we deliberately want
    # the shorter (≤5 rows when CSV has fewer than 5 data rows).
    rows = [row[:_MAX_TABLE_COLUMNS] for _, row in zip(range(5), reader, strict=False)]
    return [(None, _to_markdown_table(header, rows))]


def _extract_xlsx(data: bytes) -> SectionList:
    try:
        from openpyxl import load_workbook
    except Exception as e:
        raise ExtractError(f"openpyxl load failed: {e}") from e
    _guard_zip_bomb(data)
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            raise ExtractError("xlsx has no active sheet")
        rows_iter = ws.iter_rows(values_only=True)
    except ExtractError:
        raise
    except Exception as e:
        raise ExtractError(f"xlsx parse failed: {e}") from e
    try:
        header = list(next(rows_iter))
    except StopIteration:
        raise ExtractError("empty xlsx") from None
    header = [str(h) if h is not None else "" for h in header][:_MAX_TABLE_COLUMNS]
    sample = []
    # strict=False: range(5) caps the iteration; we deliberately want
    # the shorter (≤5 rows when sheet has fewer than 5 data rows).
    for _, row in zip(range(5), rows_iter, strict=False):
        sample.append([str(c) if c is not None else "" for c in row][:_MAX_TABLE_COLUMNS])
    return [(None, _to_markdown_table(header, sample))]


_MAX_IPYNB_BYTES = 50 * 1024 * 1024  # matches the upload cap; whole-file json.loads


def _extract_ipynb(data: bytes) -> SectionList:
    # Guard the whole-file json.loads: the web layer already caps uploads
    # at 50 MB, but enforce it here too so a fetched ipynb (or a future
    # caller with a larger cap) can't drive an unbounded parse.
    if len(data) > _MAX_IPYNB_BYTES:
        raise ExtractError(f"ipynb exceeds max size ({len(data)} > {_MAX_IPYNB_BYTES} bytes)")
    try:
        nb = json.loads(data.decode("utf-8", errors="replace"))
    except Exception as e:
        raise ExtractError(f"ipynb parse failed: {e}") from e

    sections: SectionList = []
    stack: list[tuple[int, str]] = []
    current_lines: list[str] = []
    current_path: str | None = None

    def flush() -> None:
        nonlocal current_lines
        body = "\n".join(current_lines).strip()
        if body:
            sections.append((current_path, body))
        current_lines = []

    for cell in nb.get("cells", []):
        kind = cell.get("cell_type")
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        if kind == "markdown":
            # Each markdown cell may have headings; reuse markdown logic.
            for line in source.splitlines():
                m = _MD_HEADING_RE.match(line)
                if m:
                    flush()
                    depth = len(m.group(1))
                    title = m.group(2).strip()
                    stack = [(d, t) for (d, t) in stack if d < depth]
                    stack.append((depth, title))
                    current_path = " > ".join(t for (_, t) in stack)
                else:
                    current_lines.append(line)
        elif kind == "code":
            # Include first 200 chars + any text outputs.
            current_lines.append(source[:200])
            for out in cell.get("outputs", []) or []:
                if out.get("output_type") == "stream":
                    current_lines.append(str(out.get("text", "")))
    flush()
    if not sections:
        return [(None, "")]
    return sections


# ----- helpers --------------------------------------------------------------


def _escape_md_cell(value: str) -> str:
    """Make a cell value safe to embed between ``" | "`` separators.

    A raw ``|`` would be parsed as a column boundary and a newline would
    terminate the table row, corrupting the rendered table the agent
    sees. Escape pipes and flatten embedded newlines/carriage returns to
    spaces."""
    return (
        value.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _to_markdown_table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header = [_escape_md_cell(h) for h in header]
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        cells = [_escape_md_cell(c) for c in row]
        # Pad/truncate to header length
        cells = (cells + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
