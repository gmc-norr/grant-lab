"""Shared neutralization for untrusted reference text injected into LLM prompts.

The implementation lives in ``ports/trust.py`` and is re-exported here, under
the names the resolver, the citation auditor and the claim-analysis verdict
prompt already import. It had to move when the reference TOOLS started wrapping
their own output (review 2026-10-26 M-PA-3): ``tools/`` may import only the
standard library and ``ports/`` (F3c), and a second copy of the neutralization
on the tool side is exactly the drift that turns into a prompt-injection hole.

So: one implementation, two import paths. Prefer ``ports.trust`` in new code.
"""

from __future__ import annotations

from grant_lab.proposal_assist.ports.trust import (
    _TRUST_DELIM_RE,
    UNTRUSTED_REFERENCE_NOTICE,
    UNTRUSTED_REFERENCE_TAG,
    neutralize_trust_delimiters,
    wrap_untrusted_reference,
)

__all__ = [
    "UNTRUSTED_REFERENCE_NOTICE",
    "UNTRUSTED_REFERENCE_TAG",
    "_TRUST_DELIM_RE",
    "neutralize_trust_delimiters",
    "wrap_untrusted_reference",
]
