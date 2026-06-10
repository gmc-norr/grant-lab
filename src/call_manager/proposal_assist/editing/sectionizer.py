"""Pure split/join of proposal.md into ##-headed sections.

Invariant: ``"".join(s.text for s in split_sections(doc)) == doc`` —
byte-identical. Section 0 is the preamble (possibly empty). A boundary is a
line starting with exactly ``##`` (not ``###``) outside fenced code blocks.
No I/O, no DB.

The ``##`` boundary rule is an approximation of CommonMark (backtick fences
only, no tilde fences; ``##NoSpace`` accepted; indented headings not
recognized) — exact enough for proposal.md content this app writes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_FENCE_RE = re.compile(r"^\s*```")
_H2_RE = re.compile(r"^##(?!#)\s*(?P<heading>.*?)\s*$")


@dataclass(frozen=True)
class Section:
    index: int
    heading: str  # "" for the preamble
    text: str  # exact bytes: heading line + body, including newlines

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def split_sections(text: str) -> list[Section]:
    chunks: list[tuple[str, list[str]]] = [("", [])]  # (heading, lines)
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            chunks[-1][1].append(line)
            continue
        m = None if in_fence else _H2_RE.match(stripped)
        if m:
            chunks.append((m.group("heading"), [line]))
        else:
            chunks[-1][1].append(line)
    return [
        Section(index=i, heading=heading, text="".join(lines))
        for i, (heading, lines) in enumerate(chunks)
    ]
