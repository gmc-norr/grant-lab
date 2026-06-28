import { useState } from "react";

import { Box, Button, Group, Stack, Text } from "@mantine/core";

import { useAddCitation, useReferenceChunks, useReferences } from "../api/citations";
import { ApiError } from "../api/client";
import { toCodePointOffset } from "./selection";
import styles from "./CockpitPanels.module.css";

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
  // Offsets are computed with a Range from the <pre> start to the selection
  // anchor/focus, so they are robust to split or normalised text nodes rather
  // than relying on `range.startOffset` which is only valid for single-text-node
  // containers.  We still require that both endpoints live INSIDE the <pre> so we
  // never send cross-chunk offsets the server would 422.
  function captureSelection(chunkIndex: number, chunkText: string, pre: HTMLElement) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    const quote = sel.toString();
    if (!quote) {
      setSpan(null);
      return;
    }
    // Guard: both endpoints must be descendants of (or equal to) this <pre>.
    if (!pre.contains(range.startContainer) || !pre.contains(range.endContainer)) {
      setSpan(null);
      return;
    }
    // Measure UTF-16 offset from the start of <pre> to each selection endpoint
    // via a Range, then convert to code-point offsets for the server.
    const measureUtf16Offset = (container: Node, offsetInNode: number): number => {
      const measureRange = document.createRange();
      measureRange.setStart(pre, 0);
      measureRange.setEnd(container, offsetInNode);
      return measureRange.toString().length;
    };
    const startUtf16 = measureUtf16Offset(range.startContainer, range.startOffset);
    const endUtf16 = measureUtf16Offset(range.endContainer, range.endOffset);
    const start = toCodePointOffset(chunkText, startUtf16);
    const end = toCodePointOffset(chunkText, endUtf16);
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
    <Box component="aside" aria-label="Add a citation" p="sm" style={{ border: "1px solid var(--app-border)", borderRadius: 8, background: "var(--app-sidebar)", boxShadow: "var(--app-shadow-sm)" }}>
      <Stack gap="sm">
        <Group gap="xs" align="baseline">
          <strong>Cite:</strong> <Text span c="dimmed" fs="italic">{claim.anchor}</Text>
          <Button size="xs" variant="subtle" aria-label="Cancel" ml="auto" onClick={onCancel}>✕</Button>
        </Group>

        {refId === null ? (
          <Stack gap="xs">
            <Text size="sm">Pick a reference:</Text>
            {refs.isLoading && <Text aria-busy="true">Loading references…</Text>}
            <Stack component="ul" gap={4} style={{ listStyle: "none", padding: 0, margin: 0 }}>
              {(refs.data ?? []).map((r) => (
                <Box component="li" key={r.id}>
                  <Button
                    variant="subtle"
                    size="xs"
                    disabled={r.text_status !== "done"}
                    onClick={() => setRefId(r.id)}
                  >
                    {r.title}
                    {r.text_status !== "done" ? " (not indexed yet)" : ""}
                  </Button>
                </Box>
              ))}
            </Stack>
          </Stack>
        ) : (
          <Stack gap="xs">
            <Button
              size="xs"
              variant="subtle"
              onClick={() => {
                setRefId(null);
                setSpan(null);
              }}
            >
              ‹ back to references
            </Button>
            <Text size="sm">Select the supporting passage:</Text>
            {chunks.isLoading && <Text aria-busy="true">Loading source…</Text>}
            {(chunks.data?.chunks ?? []).map((c) => (
              <pre
                key={c.chunk_index}
                className={styles.citeChunk}
                // single text node — do not interpolate siblings into this <pre>,
                // or captureSelection's offsets desync (the guard below would then bail).
                onMouseUp={(e) => captureSelection(c.chunk_index, c.text, e.currentTarget)}
              >
                {c.text}
              </pre>
            ))}
            {chunks.data && chunks.data.chunks.length === 0 && (
              <Text c="dimmed" size="sm">This source isn’t indexed yet.</Text>
            )}
            {span && (
              <Box className={styles.citeConfirm}>
                Quote: <q>{span.evidence_quote}</q>{" "}
                <Button size="xs" disabled={add.isPending} onClick={submit}>
                  Add citation
                </Button>
              </Box>
            )}
            {error !== null && <Text role="alert" c="red" size="sm">{error}</Text>}
          </Stack>
        )}
      </Stack>
    </Box>
  );
}
