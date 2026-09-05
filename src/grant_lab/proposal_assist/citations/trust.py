"""Shared neutralization for untrusted reference text injected into LLM prompts.

Reference bodies (agent-fetched from arbitrary URLs, or extracted from uploaded
files) are attacker-controlled. Wherever they are interpolated into a prompt
inside an ``<untrusted-reference>...</untrusted-reference>`` (or ``<system>``)
wrapper, the closing delimiter MUST be neutralized so the content cannot break
out of the wrapper and continue as if it were higher-trust conversation/system
text. This logic lives here (single source of truth) so the resolver, the
citation auditor, and the claim-analysis verdict prompt cannot drift.
"""

from __future__ import annotations

import re

# The trust delimiters a malicious body could try to forge: the real
# reference wrapper and a fake system block. Case-insensitive + whitespace
# tolerant so "< / Untrusted-Reference >" is caught too.
# `\b[^>]*` neutralizes the tag even when it carries attributes, so a forged
# `<selection verified="true">` inside an untrusted body cannot pose as a real
# wrapper. `\b` keeps it from matching look-alikes like `<systemic>`.
_TRUST_DELIM_RE = re.compile(
    r"<\s*/?\s*(?:system|untrusted-reference|selection)\b[^>]*>", re.IGNORECASE
)


def neutralize_trust_delimiters(text: str) -> str:
    """Rewrite the angle brackets of any trust-delimiter token to fullwidth
    look-alikes (``<`` -> ``＜``, ``>`` -> ``＞``). The text stays human-readable
    but can no longer be parsed as a real delimiter."""
    return _TRUST_DELIM_RE.sub(lambda m: m.group(0).replace("<", "＜").replace(">", "＞"), text)
