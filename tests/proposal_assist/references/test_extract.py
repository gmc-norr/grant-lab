"""Text extractors per MIME. Returns a list of (section_path, text)
tuples ready for chunking."""

import io

import pytest
from openpyxl import Workbook

from call_manager.proposal_assist.references.extract import (
    ExtractError,
    extract_text,
)


def test_extract_markdown_splits_by_headings():
    md = b"""# Title

Intro paragraph.

## Methods

Method 1 text.

## Results

Some results.
"""
    sections = extract_text(mime="text/markdown", data=md)
    paths = [s[0] for s in sections]
    assert "Title" in paths
    assert "Title > Methods" in paths
    assert "Title > Results" in paths


def test_extract_plain_text_single_section():
    sections = extract_text(mime="text/plain", data=b"just some text")
    assert sections == [(None, "just some text")]


def test_extract_rtf_strips_formatting():
    rtf = rb"{\rtf1\ansi Hello \b world\b0 .}"
    sections = extract_text(mime="application/rtf", data=rtf)
    assert len(sections) == 1
    assert "Hello" in sections[0][1] and "world" in sections[0][1]


def test_extract_docx_uses_heading_styles():
    from docx import Document

    buf = io.BytesIO()
    d = Document()
    d.add_heading("Title", level=1)
    d.add_paragraph("Intro paragraph.")
    d.add_heading("Methods", level=2)
    d.add_paragraph("Method 1 text.")
    d.save(buf)

    sections = extract_text(
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=buf.getvalue(),
    )
    paths = [s[0] for s in sections]
    assert "Title" in paths
    assert "Title > Methods" in paths


def test_extract_csv_returns_schema_and_sample():
    csv = b"name,age,city\nalice,30,paris\nbob,25,berlin\ncarol,40,london\n"
    sections = extract_text(mime="text/csv", data=csv)
    assert len(sections) == 1
    section_path, text = sections[0]
    assert section_path is None
    assert "name" in text and "age" in text and "city" in text
    assert "alice" in text


def test_extract_csv_escapes_pipes_and_newlines_in_cells():
    """Regression: a cell value containing '|' or a newline must not
    corrupt the rendered markdown table. Pipes are escaped to '\\|' and
    embedded newlines flattened to spaces so each row stays a single,
    well-formed table row."""
    # Quoted field with an embedded pipe AND an embedded newline.
    csv = b'name,note\nalice,"a|b\nsecond line"\n'
    sections = extract_text(mime="text/csv", data=csv)
    _, text = sections[0]
    lines = text.splitlines()
    # header + separator + exactly one data row — the embedded newline
    # must NOT have split the cell into a second table row.
    assert len(lines) == 3
    data_row = lines[2]
    # Pipe is escaped, not left raw (which would read as a new column).
    assert "a\\|b" in data_row
    # Newline flattened to a space.
    assert "second line" in data_row
    # The data row still has exactly the right number of column
    # separators (leading "| ", one inner " | ", trailing " |").
    assert data_row.count(" | ") == 1


def test_extract_xlsx_returns_schema_and_sample():
    buf = io.BytesIO()
    wb = Workbook()
    ws = wb.active
    ws.append(["name", "age", "city"])
    ws.append(["alice", 30, "paris"])
    ws.append(["bob", 25, "berlin"])
    wb.save(buf)

    sections = extract_text(
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        data=buf.getvalue(),
    )
    assert len(sections) == 1
    section_path, text = sections[0]
    assert section_path is None
    assert "name" in text and "alice" in text


def test_extract_pdf_returns_one_section_per_page():
    # Use a tiny valid PDF — generate with reportlab? Cheaper: hardcode
    # a one-page minimal PDF.
    pdf_bytes = _make_one_page_pdf("Hello world\nthis is page one")
    sections = extract_text(mime="application/pdf", data=pdf_bytes)
    assert len(sections) >= 1
    paths = [s[0] for s in sections]
    assert all(p.startswith("Page ") for p in paths)
    full_text = " ".join(s[1] for s in sections)
    assert "Hello world" in full_text


def test_extract_ipynb_splits_markdown_cells_as_sections():
    import json

    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Analysis\n", "intro"]},
            {
                "cell_type": "code",
                "source": ["print('hi')"],
                "outputs": [{"output_type": "stream", "text": "hi\n"}],
            },
            {"cell_type": "markdown", "source": ["## Results\n", "good"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    sections = extract_text(
        mime="application/x-ipynb+json",
        data=json.dumps(nb).encode(),
    )
    paths = [s[0] for s in sections]
    assert "Analysis" in paths
    assert "Analysis > Results" in paths


def test_extract_corrupted_pdf_raises():
    with pytest.raises(ExtractError):
        extract_text(mime="application/pdf", data=b"not a pdf at all")


def test_extract_unsupported_mime_raises():
    with pytest.raises(ExtractError, match="unsupported mime"):
        extract_text(mime="application/octet-stream", data=b"x")


def _make_zip_bomb(member_uncompressed: int) -> bytes:
    """Build a tiny ZIP whose single member declares a large uncompressed
    size but compresses to a handful of bytes (highly compressible)."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\x00" * member_uncompressed)
    return buf.getvalue()


def test_extract_docx_rejects_decompression_bomb():
    """A ZIP container whose declared uncompressed size exceeds the cap
    is refused BEFORE python-docx tries to inflate it."""
    bomb = _make_zip_bomb(300 * 1024 * 1024)  # 300 MB > 200 MB cap
    # Small on-disk size, huge declared uncompressed size.
    assert len(bomb) < 1 * 1024 * 1024
    with pytest.raises(ExtractError, match="uncompressed"):
        extract_text(
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data=bomb,
        )


def test_extract_xlsx_rejects_decompression_bomb():
    bomb = _make_zip_bomb(300 * 1024 * 1024)
    with pytest.raises(ExtractError, match="uncompressed"):
        extract_text(
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            data=bomb,
        )


def test_extract_docx_rejects_absurd_entry_count():
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i in range(20_001):
            zf.writestr(f"f{i}.xml", b"x")
    with pytest.raises(ExtractError, match="entr"):
        extract_text(
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            data=buf.getvalue(),
        )


def test_extract_xlsx_no_active_sheet_raises():
    """When openpyxl reports no active worksheet, raise ExtractError
    instead of an AttributeError on None.iter_rows."""
    import openpyxl

    class _FakeWB:
        active = None

    def _fake_load_workbook(*args, **kwargs):
        return _FakeWB()

    orig = openpyxl.load_workbook
    openpyxl.load_workbook = _fake_load_workbook
    try:
        # Build a real (tiny) valid xlsx so the bomb-check passes; the
        # patched load_workbook then returns a wb with active=None.
        buf = io.BytesIO()
        wb = Workbook()
        wb.active.append(["a"])
        wb.save(buf)
        with pytest.raises(ExtractError, match="no active sheet"):
            extract_text(
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                data=buf.getvalue(),
            )
    finally:
        openpyxl.load_workbook = orig


def test_extract_csv_caps_header_columns():
    # 200 columns in header; only first 100 should be kept.
    header = ",".join(f"c{i}" for i in range(200)).encode()
    csv = header + b"\n" + b",".join(b"v" for _ in range(200)) + b"\n"
    sections = extract_text(mime="text/csv", data=csv)
    _, text = sections[0]
    header_line = text.splitlines()[0]
    # Header cells are separated by " | "; count columns.
    n_cols = header_line.count(" | ") + 1
    assert n_cols == 100


def _make_one_page_pdf(text: str) -> bytes:
    """Generate a minimal one-page PDF without requiring reportlab.
    Uses pdfminer's pdfwriter? Actually simplest is to ship a fixture
    file. For testability we'll use a hardcoded small PDF — see fixture."""
    # The smallest valid PDF that pdfminer can parse with text is non-trivial
    # to hand-craft. We use reportlab as a test-time helper.
    from io import BytesIO

    from reportlab.pdfgen.canvas import Canvas

    buf = BytesIO()
    c = Canvas(buf)
    for line in text.split("\n"):
        c.drawString(72, 720, line)
        c.translate(0, -14)
    c.save()
    return buf.getvalue()
