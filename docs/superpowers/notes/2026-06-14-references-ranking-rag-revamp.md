# Future work: references + ranking RAG revamp

**Status:** agenda only — captured to seed a later brainstorm → spec → plan. NOT a design.
**Date:** 2026-06-14

## Context / why this note exists

Reference upload broke in the dev env: ingestion 400'd at the embed step
(`mxbai-embed-large` on Ollama 0.21 rejects dense chunks >~1200 chars —
`"input length exceeds the context length"`; `truncate:true` doesn't help; the
same Ollama 0.21 also returns NaN for `bge-m3` on long inputs). We agreed to a
two-track approach: fix the immediate bug now, think about the architecture
properly later.

## Track 1 — DONE (2026-06-14)

Root-cause, not a rewrite:

1. **Embed client shrink-and-retry** (`embeddings/client.py`): on a length-related
   failure — a 400 "exceeds context length" *or* NaN components (both are
   long-input problems) — halve the input and retry down to a 256-char floor,
   then fail loud. Makes embedding robust to any over-long input regardless of
   model/serving quirks. Shared with ranking (strictly positive there too).
2. **Conservative reference chunk size** (`references/ingest.py`:
   `_REFERENCE_CHUNK_CHARS = 1000`): keeps chunks inside small embedders' context
   so retries (and the content loss they cause) stay rare. Char-budgeted, not
   token-budgeted — see Track 2.

Verified end-to-end through the real app pipeline (upload → `done`, 257 chunks).
Tests: 4 new embed-client tests; 127 embed/ranking + 274 references tests green.

## Track 2 — the revamp (deliberately deferred)

Agenda for the deep session. Goal: performance, quality, maintainability.

- **Serving.** Ollama-for-embeddings is fragile (NaN, 400, broken truncate on
  0.21). Evaluate: upgrade Ollama vs. a dedicated embedding server (HF
  text-embeddings-inference / sentence-transformers / vLLM) with correct
  truncation + batching. Embeddings don't have to share Ollama with the LLM.
- **Chunking.** Replace the char budget with a **token-budgeted, model-derived**
  size (chunk to the embedder's real context window), removing the magic 1000.
- **Embedding model.** Multilingual + long-context for SE/EN medical text
  (`mxbai` is English-leaning). Candidates: bge-m3, EmbeddingGemma,
  Qwen3-Embedding. Choose on a **small SE+EN eval harness**, not vibes.
- **Retrieval quality.** Dense-only → hybrid (dense + BM25) + a **cross-encoder
  reranker** (bge-reranker-v2-m3). Citation precision matters: we want the right
  chunk to quote.
- **Maintainability (the big structural win).** `references/` and `ranking/` each
  carry their own embedding path. Unify behind **one embedding+rerank service** —
  which is exactly the deferred v2-ranker hybrid (see
  `project_v2_ranker_hybrid` memory). The revamp should *converge* the two, not
  add a third stack.
- **Keep:** the verbatim-evidence citation validation — it's the principled core.

## Open questions
- One embedding model for both ranking and references, or different (ranking =
  short profile/call text; references = long paper chunks)?
- Re-embed cost/migration when the model/dim changes (pgvector column dim,
  backfill) — coordinate with the existing daily re-embed tick.
- Does the user's *production* serving differ from this dev Ollama 0.21? The bug
  may not reproduce there; confirm before over-fitting fixes to 0.21.
