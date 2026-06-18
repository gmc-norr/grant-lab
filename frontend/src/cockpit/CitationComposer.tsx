import { useState } from "react";

import { useAddCitation, useReferenceChunks, useReferences } from "../api/citations";
import { ApiError } from "../api/client";
import { toCodePointOffset } from "./selection";

export type ClaimContext = {
  section_index: number;
  base_commit_sha: string;
  base_section_sha: string;
  anchor: string;
};

type Props = {
  workspaceId: string;
  claim: ClaimContext;
  onDone: () => void;
  onCancel: () => void;
};

type Span = { chunk_index: number; quote_start: number; quote_end: number; evidence_quote: string };

/** Right-pane select-to-cite composer: pick a reference, select a supporting
 *  span in its (verbatim) chunk text, confirm -> POST a direct citation. */
export function CitationComposer({ workspaceId, claim, onDone, onCancel }: Props) {
  const refs = useReferences(workspaceId);
  const [refId, setRefId] = useState<string | null>(null);
  const chunks = useReferenceChunks(workspaceId, refId);
  const [span, setSpan] = useState<Span | null>(null);
  const add = useAddCitation(workspaceId);
  const [error, setError] = useState<string | null>(null);

  // Read the DOM selection inside a chunk <pre>; store chunk-local code-point offsets.
  function captureSelection(chunkIndex: number, chunkText: string) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    const quote = sel.toString();
    if (!quote) {
      setSpan(null);
      return;
    }
    const start = toCodePointOffset(chunkText, range.startOffset);
    const end = toCodePointOffset(chunkText, range.endOffset);
    const [lo, hi] = start <= end ? [start, end] : [end, start];
    setSpan({ chunk_index: chunkIndex, quote_start: lo, quote_end: hi, evidence_quote: quote });
  }

  async function submit() {
    if (!span) return;
    setError(null);
    try {
      await add.mutateAsync({
        section_index: claim.section_index,
        base_commit_sha: claim.base_commit_sha,
        base_section_sha: claim.base_section_sha,
        anchor: claim.anchor,
        reference_id: refId as string,
        chunk_index: span.chunk_index,
        evidence_quote: span.evidence_quote,
        quote_start: span.quote_start,
        quote_end: span.quote_end,
        stance: "supports",
      });
      onDone();
    } catch (e) {
      setError(
        e instanceof ApiError
          ? e.message.replace(`${e.code}: `, "")
          : "Could not add the citation — please try again.",
      );
    }
  }

  return (
    <aside className="cite-composer" aria-label="Add a citation">
      <header className="cite-composer-head">
        <strong>Cite:</strong> <span className="cite-composer-claim">{claim.anchor}</span>
        <button type="button" aria-label="Cancel" onClick={onCancel}>
          ✕
        </button>
      </header>

      {refId === null ? (
        <div className="cite-composer-refs">
          <p>Pick a reference:</p>
          {refs.isLoading && <p aria-busy="true">Loading references…</p>}
          <ul>
            {(refs.data ?? []).map((r) => (
              <li key={r.id}>
                <button
                  type="button"
                  disabled={r.text_status !== "done"}
                  onClick={() => setRefId(r.id)}
                >
                  {r.title}
                  {r.text_status !== "done" ? " (not indexed yet)" : ""}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="cite-composer-source">
          <button
            type="button"
            onClick={() => {
              setRefId(null);
              setSpan(null);
            }}
          >
            ‹ back to references
          </button>
          <p>Select the supporting passage:</p>
          {chunks.isLoading && <p aria-busy="true">Loading source…</p>}
          {(chunks.data?.chunks ?? []).map((c) => (
            <pre
              key={c.chunk_index}
              className="cite-composer-chunk"
              onMouseUp={() => captureSelection(c.chunk_index, c.text)}
            >
              {c.text}
            </pre>
          ))}
          {chunks.data && chunks.data.chunks.length === 0 && (
            <p className="cite-composer-empty">This source isn’t indexed yet.</p>
          )}
          {span && (
            <p className="cite-composer-confirm">
              Quote: <q>{span.evidence_quote}</q>{" "}
              <button type="button" disabled={add.isPending} onClick={submit}>
                Add citation
              </button>
            </p>
          )}
          {error !== null && <p role="alert">{error}</p>}
        </div>
      )}
    </aside>
  );
}
