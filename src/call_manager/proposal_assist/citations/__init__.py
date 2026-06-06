"""Citation subsystem. The ONLY supported LLM entry point is
CitationLLMService / build_citation_service — do not call the structured
client directly (it bypasses caps + the usage ledger)."""

from call_manager.proposal_assist.citations.service import (
    CitationLLMResult,
    CitationLLMService,
    build_citation_service,
)

__all__ = ["CitationLLMResult", "CitationLLMService", "build_citation_service"]
