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
from collections.abc import Iterable


class ExtractError(RuntimeError):
    """Raised when text extraction fails for any reason."""


SectionList = list[tuple[str | None, str]]


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
    # strict=False: range(5) caps the iteration; we deliberately want
    # the shorter (≤5 rows when CSV has fewer than 5 data rows).
    rows = [row for _, row in zip(range(5), reader, strict=False)]
    return [(None, _to_markdown_table(header, rows))]


def _extract_xlsx(data: bytes) -> SectionList:
    try:
        from openpyxl import load_workbook
    except Exception as e:
        raise ExtractError(f"openpyxl load failed: {e}") from e
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        raise ExtractError(f"xlsx parse failed: {e}") from e
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header = list(next(rows_iter))
    except StopIteration:
        raise ExtractError("empty xlsx") from None
    header = [str(h) if h is not None else "" for h in header]
    sample = []
    # strict=False: range(5) caps the iteration; we deliberately want
    # the shorter (≤5 rows when sheet has fewer than 5 data rows).
    for _, row in zip(range(5), rows_iter, strict=False):
        sample.append([str(c) if c is not None else "" for c in row])
    return [(None, _to_markdown_table(header, sample))]


def _extract_ipynb(data: bytes) -> SectionList:
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


def _to_markdown_table(header: Iterable[str], rows: Iterable[Iterable[str]]) -> str:
    header = list(header)
    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        cells = list(row)
        # Pad/truncate to header length
        cells = (cells + [""] * len(header))[: len(header)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
