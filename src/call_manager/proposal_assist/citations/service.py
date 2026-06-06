"""The ONLY supported citation-LLM call path.

Every call is cap-checked before and ledger-recorded after (ok/failed/timeout),
so no caller can spend un-accounted tokens or skip the cap. Returns a typed
cap-exceeded result WITHOUT invoking the client when over budget.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import sessionmaker

from call_manager.config import Settings
from call_manager.proposal_assist.citations.llm import LLMUsage, StructuredLLM
from call_manager.proposal_assist.citations.usage import check_citation_caps, record_usage

logger = logging.getLogger(__name__)


@dataclass
class CitationLLMResult:
    data: dict | None
    usage: LLMUsage | None
    model: str | None
    cap_exceeded: bool = False
    cap_scope: str | None = None


class CitationLLMService:
    def __init__(
        self, *, session_factory: sessionmaker, llm: StructuredLLM, settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings

    def complete_json(
        self, *, workspace_id: uuid.UUID, op: str, system: str, user: str
    ) -> CitationLLMResult:
        if not op or len(op) > 64:
            raise ValueError(f"invalid citation LLM op: {op!r}")
        with self._session_factory() as s:
            cap = check_citation_caps(s, workspace_id, settings=self._settings)
        if cap.exceeded:
            return CitationLLMResult(
                data=None, usage=None, model=None, cap_exceeded=True, cap_scope=cap.scope
            )

        try:
            result = self._llm.complete_json(system=system, user=user)
        except TimeoutError:
            # NOTE: OllamaClient/AnthropicClient currently wrap transport
            # timeouts as LLMError (httpx.TimeoutException does NOT subclass
            # builtin TimeoutError), so this branch only fires for clients that
            # propagate TimeoutError directly. Refinement deferred to Slice 3.
            self._safe_record(workspace_id, op, "timeout", 0, 0, None)
            raise
        except Exception:
            self._safe_record(workspace_id, op, "failed", 0, 0, None)
            raise

        # Success: record usage. If the ledger write fails here the tokens were
        # already spent, so we log loudly but still return the valid result
        # rather than crash the caller (which would otherwise retry and double-spend).
        ok = self._safe_record(
            workspace_id, op, "ok", result.usage.tokens_in, result.usage.tokens_out, result.model
        )
        if not ok:
            logger.error(
                "citation ledger write FAILED after a successful LLM call — "
                "tokens spent but unaccounted (workspace=%s op=%s)",
                workspace_id,
                op,
            )
        return CitationLLMResult(data=result.data, usage=result.usage, model=result.model)

    def _safe_record(self, workspace_id, op, status, tokens_in, tokens_out, model) -> bool:
        try:
            with self._session_factory() as s:
                record_usage(
                    s,
                    workspace_id=workspace_id,
                    op=op,
                    status=status,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    model=model,
                )
                s.commit()
            return True
        except Exception:
            logger.warning(
                "citation ledger write failed (workspace=%s op=%s status=%s)",
                workspace_id,
                op,
                status,
                exc_info=True,
            )
            return False


def build_citation_service(
    *, settings: Settings, session_factory: sessionmaker
) -> CitationLLMService:
    """The supported way to obtain a citation LLM caller. Builds the structured
    client from settings and wraps it in the caps+ledger-enforcing service."""
    from call_manager.proposal_assist.citations.llm import build_citation_llm

    return CitationLLMService(
        session_factory=session_factory, llm=build_citation_llm(settings), settings=settings
    )
