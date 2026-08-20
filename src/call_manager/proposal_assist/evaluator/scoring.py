"""Deterministic overall-readiness roll-up. The agent judges each criterion's
band; the server computes ONE overall band from the per-criterion bands weighted
by effective weight, with a gap floor (a pure average can read 'strong' while a
meaningful criterion is a gap). Pure — no DB, no LLM, no I/O."""

from __future__ import annotations

from call_manager.proposal_assist.ports.scoring import (
    compute_overall_band as compute_overall_band,
)
