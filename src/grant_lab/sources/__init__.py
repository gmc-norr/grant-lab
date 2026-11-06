"""Source adapters for funding-call ingestion.

Each adapter normalizes one funding-body's API into `NormalizedCall`
([sources/types.py](./types.py)) and yields them via `fetch_new_calls(since)`.

## Populating `NormalizedCall.full_text` — preferred order

When writing a new adapter, populate `full_text` in this order of preference:

1. **Use the source's API** if it returns the call body (e.g., Vinnova's
   `WebTextLista`). Fastest, most stable, structured. If the source ships the
   body in several languages, prefer English — the ranker is English-primary.

2. **Fetch the source's call-detail endpoint** if it's a separate API call
   (some funding bodies have list+detail endpoints; the body is in detail).

3. **Return None** if none of the above apply. Ranking still works
   (degraded — the embedding gate becomes UNKNOWN, the LLM falls back to
   title + summary alone).

The orchestrator does NOT scrape. This keeps source-specific intent inside the
source's adapter and the orchestrator stays source-agnostic. (A generic HTML/PDF
scraping helper, `sources/extraction.py`, was removed on 2026-11-05: no adapter
used it.)

## Failure handling

The upsert layer preserves `full_text` against transient None — see
`ingestion/upsert.py`. Adapters can return `full_text=None` on a failed
scrape without erasing previously-captured body text on existing rows.

## Ranking pipeline impact

Once `full_text` is populated, three downstream layers use it automatically:

- `embeddings.similarity.build_call_text` includes `full_text[:8000]` in
  BGE-M3 input.
- `ranking.prompt.build_messages` includes `summary + full_text[:CALL_TEXT_LIMIT]`
  (16000) in the LLM rubric body.
- `ranking.rank_pair._build_verification_text` uses untruncated `full_text`
  for quoted-evidence verification.

There is no schema migration required for new adapters — `Call.full_text`
already exists.
"""
