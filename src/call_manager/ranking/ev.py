# src/call_manager/ranking/ev.py
"""G-Eval expected-value extraction for the topic_fit rating.

Converts the LLM's single integer topic_fit rating into an expected value
over the rating token's top-k logprobs, recovering a continuous score from a
6-point Likert scale. Deterministic JSON-offset binding (Codex R7) locates
the structural `topic_fit.rating` literal so we never weight a digit that
happens to appear in rationale prose. Any inconsistency returns None and the
caller falls back to argmax — this function never raises.
"""

from __future__ import annotations

import math

_DIGITS = {"0", "1", "2", "3", "4", "5"}


def extract_topic_expected(envelope: dict, parsed_rating: int) -> float | None:
    try:
        choice = envelope["choices"][0]
        content: str = choice["message"]["content"]
        tokens = choice["logprobs"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(tokens, list) or not content:
        return None

    rating_offset = _locate_rating_literal_offset(content)
    if rating_offset is None:
        return None

    covering = _token_covering_offset(tokens, rating_offset)
    if covering is None:
        return None
    # Sanity: the covering token must be exactly the parsed rating digit.
    if covering.get("token") != str(parsed_rating):
        return None

    top = covering.get("top_logprobs")
    if not isinstance(top, list):
        return None
    weights: dict[int, float] = {}
    for entry in top:
        tok = entry.get("token") if isinstance(entry, dict) else None
        lp = entry.get("logprob") if isinstance(entry, dict) else None
        if tok in _DIGITS and isinstance(lp, (int, float)):
            weights[int(tok)] = weights.get(int(tok), 0.0) + math.exp(lp)
    if len(weights) < 2:
        return None  # degenerate — EV == argmax, not worth recording
    total = sum(weights.values())
    if total <= 0:
        return None
    ev = sum(d * w for d, w in weights.items()) / total
    if not math.isfinite(ev) or not 0.0 <= ev <= 5.0:
        return None
    return ev


def _token_covering_offset(tokens: list, target: int) -> dict | None:
    """Walk tokens accumulating character offsets (from each token's UTF-8
    bytes, falling back to its text) and return the token whose span covers
    `target`."""
    pos = 0
    for tok in tokens:
        if not isinstance(tok, dict):
            return None
        raw = tok.get("bytes")
        if isinstance(raw, list):
            try:
                text = bytes(raw).decode("utf-8")
            except (ValueError, TypeError):
                text = tok.get("token", "")
        else:
            text = tok.get("token", "")
        end = pos + len(text)
        if pos <= target < end:
            return tok
        pos = end
    return None


def _locate_rating_literal_offset(content: str) -> int | None:
    """Return the character offset of the integer literal that is the VALUE of
    the structural `topic_fit.rating` key. String-literal-aware: ignores any
    `"rating"` text that sits inside a JSON string value.

    Minimal scanner: walk the chars tracking whether we're inside a string
    (respecting backslash escapes). Outside strings, detect the key sequence
    `"topic_fit"` then, after it, the key `"rating"` followed by `:` and an
    integer literal; return the digit's offset.
    """
    n = len(content)
    i = 0
    in_string = False
    escape = False
    saw_topic_fit = False
    # Track the most recent completed string-literal (key candidate).
    last_key: str | None = None
    key_start = -1
    while i < n:
        ch = content[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
                last_key = content[key_start + 1 : i]
            i += 1
            continue
        if ch == '"':
            in_string = True
            key_start = i
            i += 1
            continue
        if ch == ":" and last_key is not None:
            key = last_key
            last_key = None
            if key == "topic_fit":
                saw_topic_fit = True
            elif key == "rating" and saw_topic_fit:
                # Skip whitespace to the integer literal.
                j = i + 1
                while j < n and content[j] in " \t\n\r":
                    j += 1
                if j < n and content[j].isdigit():
                    return j
                return None
        i += 1
    return None
