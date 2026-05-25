"""Section-aware chunker: produces (section_path, text) chunks ready
for embedding.

Strategy: one chunk per input section when small enough; if any section
exceeds max_chunk_chars, sub-chunk recursively with overlap. section_path
is preserved across sub-chunks so the agent always knows the source
context.
"""

from __future__ import annotations


def chunk_sections(
    sections: list[tuple[str | None, str]],
    *,
    max_chunk_chars: int = 1500,
    overlap_chars: int = 100,
) -> list[tuple[str | None, str]]:
    """Convert a list of extracted sections into a list of chunks.

    sections: output of extract.extract_text(); list of (section_path, body).
    Returns a list of (section_path, chunk_text) where each chunk_text
    is at most max_chunk_chars long.
    """
    out: list[tuple[str | None, str]] = []
    for section_path, body in sections:
        body = body.strip()
        if not body:
            continue
        if len(body) <= max_chunk_chars:
            out.append((section_path, body))
            continue
        # Sub-chunk recursively.
        for sub in _split_with_overlap(body, max_chunk_chars, overlap_chars):
            out.append((section_path, sub))
    return out


def _split_with_overlap(text: str, size: int, overlap: int) -> list[str]:
    """Plain character-based split with overlap. Boring but reliable."""
    if overlap >= size:
        raise ValueError("overlap must be less than size")
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks
