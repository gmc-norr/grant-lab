"""Pure split/join of proposal.md into ##-headed sections.

Invariant: ``"".join(s.text for s in split_sections(doc)) == doc`` —
byte-identical. Section 0 is the preamble (possibly empty). A boundary is a
line starting with exactly ``##`` (not ``###``) outside fenced code blocks.
No I/O, no DB.

The ``##`` boundary rule is an approximation of CommonMark (backtick fences
only, no tilde fences; ``##NoSpace`` accepted; indented headings not
recognized) — exact enough for proposal.md content this app writes. Fence
LENGTH is honoured: a fence closes only on a backtick run at least as long as
the one that opened it, with nothing after it — so a code block that contains
a line of three backticks (which the collab serializer wraps in a longer fence)
does not end early and swallow every later heading (review 2026-09-02 §2.3).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^\s*(?P<run>`{3,})(?P<rest>.*)$")
_H2_RE = re.compile(r"^##(?!#)\s*(?P<heading>.*?)\s*$")


@dataclass(frozen=True)
class Section:
    index: int
    heading: str  # "" for the preamble
    text: str  # exact bytes: heading line + body, including newlines

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def is_h2_heading_line(line: str) -> bool:
    """Return True if *line* is an H2 boundary (``## …``, not ``###``).

    Thin public wrapper around ``_H2_RE`` for callers that need the check
    outside the fence-tracking context of :func:`split_sections`.
    """
    return bool(_H2_RE.match(line.rstrip("\n")))


def split_sections(text: str) -> list[Section]:
    chunks: list[tuple[str, list[str]]] = [("", [])]  # (heading, lines)
    fence = 0  # backtick-run length of the open fence; 0 while outside one
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        f = _FENCE_RE.match(stripped)
        if f:
            run = len(f.group("run"))
            if not fence:
                fence = run  # an opening fence may carry an info string
                chunks[-1][1].append(line)
                continue
            if run >= fence and not f.group("rest").strip():
                fence = 0  # a closing fence: at least as long, nothing after it
                chunks[-1][1].append(line)
                continue
            # any other backtick line inside a fence is code, not a boundary
        m = None if fence else _H2_RE.match(stripped)
        if m:
            chunks.append((m.group("heading"), [line]))
        else:
            chunks[-1][1].append(line)
    return [
        Section(index=i, heading=heading, text="".join(lines))
        for i, (heading, lines) in enumerate(chunks)
    ]
