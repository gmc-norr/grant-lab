"""What a converted tool looks like: stdlib and ports, nothing else.

PARSED by the import gate, never imported and never collected. It is here so the
rule has a worked example a reviewer can read, not only a list of prohibitions.
"""

from __future__ import annotations

import uuid

from call_manager.proposal_assist.ports import Ports
from call_manager.proposal_assist.ports.effects import SubmitScorecard
from call_manager.proposal_assist.ports.errors import EffectRejected
from call_manager.proposal_assist.ports.stamp import FenceStamp


def submit(ports: Ports, stamp: FenceStamp, scorecard: dict) -> dict:
    try:
        ports.effects.submit_scorecard(stamp, SubmitScorecard(scorecard=scorecard))
    except EffectRejected as exc:
        return {"error": exc.reason}
    return {"stored": True, "id": str(uuid.uuid4())}
