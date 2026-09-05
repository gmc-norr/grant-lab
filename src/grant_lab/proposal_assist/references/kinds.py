"""Shared reference `kind` enum (domain layer, importable by metadata.py)."""

from __future__ import annotations

ALLOWED_KINDS = frozenset({"paper", "prior_proposal", "analysis", "data"})
