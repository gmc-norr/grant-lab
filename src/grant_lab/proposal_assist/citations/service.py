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

from grant_lab.config import Settings
from grant_lab.proposal_assist.citations.llm import LLMUsage, StructuredLLM, tokens_from_usage
from grant_lab.proposal_assist.citations.usage import check_citation_caps, record_usage
from grant_lab.proposal_assist.turns.caps import check_global_daily_token_cap

logger = logging.getLogger(__name__)


@dataclass
class CitationLLMResult:
    data: dict | None
    usage: LLMUsage | None
    model: str | None
    cap_exceeded: bool = False
    cap_scope: str | None = None


class CitationLLMService:
    # After this many CONSECUTIVE post-spend ledger-write failures the cap can no
    # longer see real spend, so we stop calling the LLM (fail CLOSED) and surface
    # a cap-exceeded result until a record succeeds. A single transient failure
    # still returns its already-spent result (no double-spend on retry); only a
    # sustained inability to record trips the breaker.
    _RECORD_FAILURE_CIRCUIT_BREAK = 3

    def __init__(
        self, *, session_factory: sessionmaker, llm: StructuredLLM, settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._settings = settings
        self._consecutive_record_failures = 0

    def complete_json(
        self, *, workspace_id: uuid.UUID, op: str, system: str, user: str
    ) -> CitationLLMResult:
        if not op or len(op) > 64:
            raise ValueError(f"invalid citation LLM op: {op!r}")

        # Circuit breaker: if the ledger has been unwritable for the last N
        # successful-spend calls, the cap is blind to real usage — refuse to
        # spend more rather than fail open unbounded.
        if self._consecutive_record_failures >= self._RECORD_FAILURE_CIRCUIT_BREAK:
            logger.error(
                "citation LLM circuit-broken: %d consecutive ledger-write failures "
                "(workspace=%s op=%s) — refusing to spend while usage is unaccounted",
                self._consecutive_record_failures,
                workspace_id,
                op,
            )
            return CitationLLMResult(
                data=None,
                usage=None,
                model=None,
                cap_exceeded=True,
                cap_scope="ledger_unavailable",
            )

        with self._session_factory() as s:
            cap = check_citation_caps(s, workspace_id, settings=self._settings)
            # The GLOBAL ceiling, checked in the same session. Citation spend was
            # unified INTO the global total by this branch, but the service only ever
            # consulted its own per-op caps — making it a sixth family that adds to
            # the number and never reads it, which is the exact violation the ledger
            # exists to remove. Refused the same way as a per-op cap, so the caller's
            # existing cap_exceeded handling covers it and no call is made.
            global_cap = check_global_daily_token_cap(s, settings=self._settings)
        if cap.exceeded:
            return CitationLLMResult(
                data=None, usage=None, model=None, cap_exceeded=True, cap_scope=cap.scope
            )
        if not global_cap.allowed:
            return CitationLLMResult(
                data=None, usage=None, model=None, cap_exceeded=True, cap_scope="global"
            )

        try:
            result = self._llm.complete_json(system=system, user=user)
        except TimeoutError:
            # AnthropicClient raises AnthropicTimeoutError (an LLMError AND a
            # TimeoutError) for a timeout waiting on a sent request, which may
            # have been billed, so it lands here. OllamaClient still wraps its
            # timeouts as a plain LLMError (httpx.TimeoutException does not
            # subclass the builtin TimeoutError), which the branch below takes.
            self._note_record(self._safe_record(workspace_id, op, "timeout", 0, 0, None))
            raise
        except Exception as e:
            # An error raised after the API answered carries the billed usage
            # (AnthropicClient._billed, StructuredLLM's parse failure); 0 only
            # when the call never got an answer.
            tokens_in, tokens_out = tokens_from_usage(getattr(e, "usage", None) or {})
            self._note_record(
                self._safe_record(workspace_id, op, "failed", tokens_in, tokens_out, None)
            )
            raise

        # Success: record usage. If the ledger write fails here the tokens were
        # already spent, so we log loudly and still return the valid result
        # rather than crash the caller (which would otherwise retry and
        # double-spend) — but we COUNT the failure: a sustained streak trips the
        # circuit breaker above so spend cannot run unbounded behind a blind cap.
        ok = self._safe_record(
            workspace_id, op, "ok", result.usage.tokens_in, result.usage.tokens_out, result.model
        )
        self._note_record(ok)
        if not ok:
            logger.error(
                "citation ledger write FAILED after a successful LLM call — "
                "tokens spent but unaccounted (workspace=%s op=%s streak=%d)",
                workspace_id,
                op,
                self._consecutive_record_failures,
            )
        return CitationLLMResult(data=result.data, usage=result.usage, model=result.model)

    def _note_record(self, ok: bool) -> None:
        """Move the breaker streak for EVERY ledger write, not just the success one.

        The failure and timeout branches used to discard `_safe_record`'s result,
        so a ledger that was down for every call never tripped the breaker — the
        one mechanism that stops us calling the LLM while spend is unaccounted.
        The rows on those paths carry zero tokens, but a write failing there means
        the ledger is broken, which is exactly what the breaker is for.
        """
        if ok:
            self._consecutive_record_failures = 0
        else:
            self._consecutive_record_failures += 1

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
                    # The provider the client was BUILT from: `build_citation_llm`
                    # switched on this same value, so the tag cannot disagree
                    # with the client that spent the tokens.
                    provider=self._settings.citation_llm_provider,
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
    from grant_lab.proposal_assist.citations.llm import build_citation_llm

    return CitationLLMService(
        session_factory=session_factory, llm=build_citation_llm(settings), settings=settings
    )
