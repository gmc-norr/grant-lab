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
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        raise ValueError(f"unsupported citations.json (expected version {LEDGER_VERSION})")
    data.setdefault("evidence", [])
    if not isinstance(data["evidence"], list):
        raise ValueError("citations.json 'evidence' must be a list")
    return data


def dump_ledger(ledger: dict) -> str:
    """Serialize to stable, human-diffable JSON (sorted keys, 2-space indent, trailing newline)."""
    return json.dumps(ledger, indent=2, sort_keys=True) + "\n"


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
