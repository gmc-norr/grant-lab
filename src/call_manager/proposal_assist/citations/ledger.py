"""In-memory model for the citations.json v2 evidence ledger.

Pure helpers over a plain dict — no file I/O, git, or DB (callers own those).
The ledger maps each inline citation marker (by its stable `ev-<id>`) to the
exact chunk + quote that backs a claim, with content hashes for drift detection.
"""

from __future__ import annotations

import json
import uuid

LEDGER_VERSION = 2


def empty_ledger() -> dict:
    return {"version": LEDGER_VERSION, "evidence": []}


def load_ledger(text: str | None) -> dict:
    """Parse citations.json text. Absent/empty → empty ledger. Validates version."""
    if not text or not text.strip():
        return empty_ledger()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"citations.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        raise ValueError(f"unsupported citations.json (expected version {LEDGER_VERSION})")
    evidence = data.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("citations.json 'evidence' must be a list")
    for i, row in enumerate(evidence):
        if not isinstance(row, dict):
            raise ValueError(
                f"citations.json evidence[{i}] must be a dict, got {type(row).__name__}"
            )
    return {"version": LEDGER_VERSION, "evidence": evidence}


def dump_ledger(ledger: dict) -> str:
    """Serialize to stable, human-diffable JSON (sorted keys, 2-space indent, trailing newline).

    ``ensure_ascii=False`` keeps non-ASCII characters (e.g. accented letters in
    evidence quotes) readable in git diffs while the output remains deterministic.
    """
    return json.dumps(ledger, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def new_ev_id() -> str:
    return f"ev-{uuid.uuid4().hex[:12]}"


def find_by_id(ledger: dict, ev_id: str) -> dict | None:
    return next((e for e in ledger["evidence"] if e.get("id") == ev_id), None)


def find_by_citekey(ledger: dict, citekey: str) -> list[dict]:
    return [e for e in ledger["evidence"] if e.get("citekey") == citekey]


def citekeys_in_use(ledger: dict) -> set[str]:
    return {e["citekey"] for e in ledger["evidence"] if e.get("citekey")}


def append_evidence(ledger: dict, row: dict) -> dict:
    """Return a NEW ledger dict with row appended (does not mutate input)."""
    new = {"version": ledger.get("version", LEDGER_VERSION), "evidence": [*ledger["evidence"], row]}
    return new


def remove_evidence(ledger: dict, ev_id: str) -> tuple[dict, bool]:
    """Return (new_ledger, removed?). removed=False if ev_id not present."""
    kept = [e for e in ledger["evidence"] if e.get("id") != ev_id]
    removed = len(kept) != len(ledger["evidence"])
    return {"version": ledger.get("version", LEDGER_VERSION), "evidence": kept}, removed


def update_evidence(ledger: dict, ev_id: str, **fields) -> tuple[dict, bool]:
    """Return (new_ledger, updated?). Shallow-merges `fields` into the matching row.
    Does not mutate input rows."""
    if "id" in fields:
        raise ValueError("evidence 'id' is immutable")
    updated = False
    out = []
    for e in ledger["evidence"]:
        if e.get("id") == ev_id:
            merged = {**e, **fields}
            out.append(merged)
            updated = True
        else:
            out.append(e)
    return {"version": ledger.get("version", LEDGER_VERSION), "evidence": out}, updated


def build_evidence_row(
    *,
    ev_id: str,
    citekey: str,
    reference_id: str,
    chunk_index: int,
    section_path: str | None,
    source_content_hash: str | None,
    chunk_content_hash: str,
    evidence_quote: str,
    evidence_quote_offsets: list[int],
    stance: str,
    confidence: str | None,
    claim_context_text: str,
    claim_context_hash: str,
    citation_snapshot: str,
    created_at: str,
    updated_at: str,
) -> dict:
    """Construct a spec-exact v2 evidence row.

    All timestamps and IDs are passed in by the caller — this function contains
    NO datetime or random calls. This is the single authoritative constructor so
    all mutation tools (T5/T6/T7) produce identical schema shapes.
    """
    return {
        "id": ev_id,
        "citekey": citekey,
        "reference_id": reference_id,
        "locator": {"chunk_index": chunk_index, "section_path": section_path},
        "source_content_hash": source_content_hash,
        "chunk_content_hash": chunk_content_hash,
        "evidence_quote": evidence_quote,
        "evidence_quote_offsets": evidence_quote_offsets,
        "stance": stance,
        "confidence": confidence,
        "claim_context": {"text": claim_context_text, "hash": claim_context_hash},
        "citation_snapshot": citation_snapshot,
        "created_at": created_at,
        "updated_at": updated_at,
    }
