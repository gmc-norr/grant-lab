import { Box, Button, Group, Text } from "@mantine/core";

import type { components } from "../api/schema.gen";
import styles from "./CockpitPanels.module.css";

export type CitationSource = components["schemas"]["CitationSourceOut"];

type Props = {
  source: CitationSource;
  pinned: boolean;
  onClose: () => void;
  onPin: () => void;
  onNavigate: (evId: string) => void;
};

/** Presentational cited-source pane: highlighted evidence + nav + pin/close.
 *  Pure (no data fetching); the Cockpit owns selection + the useCitationSource
 *  query and passes the resolved source in. */
export function SourceViewer({ source, pinned, onClose, onPin, onNavigate }: Props) {
  const { text, highlight } = source;
  // Defense-in-depth: the server guarantees 0 <= start <= end <= len(text), but
  // SP2a-4b will derive these offsets client-side, so reject a reversed/out-of-
  // range span and fall back to plain text rather than silently duplicating or
  // dropping characters in a citation viewer.
  const validHighlight =
    highlight !== null &&
    highlight !== undefined &&
    highlight[0] >= 0 &&
    highlight[0] <= highlight[1] &&
    highlight[1] <= text.length
      ? highlight
      : null;
  return (
    <Box
      component="aside"
      data-testid="source-viewer"
      aria-label="Cited source"
      p="sm"
      style={{
        border: "1px solid var(--app-border)",
        borderRadius: 8,
        background: "var(--app-sidebar)",
        boxShadow: "var(--app-shadow-sm)",
        display: "flex",
        flexDirection: "column",
        gap: "0.5rem",
      }}
    >
      <Group gap="sm" align="baseline" wrap="wrap">
        <Text fw={600}>{source.source_line}</Text>
        {source.total > 0 && (
          <Text size="sm" c="dimmed">
            {source.index} of {source.total}
          </Text>
        )}
        <Group gap={4} ml="auto">
          <Button
            size="xs"
            variant="default"
            aria-label="Previous citation"
            disabled={!source.prev_ev}
            onClick={() => source.prev_ev && onNavigate(source.prev_ev)}
          >
            ‹
          </Button>
          <Button
            size="xs"
            variant="default"
            aria-label="Next citation"
            disabled={!source.next_ev}
            onClick={() => source.next_ev && onNavigate(source.next_ev)}
          >
            ›
          </Button>
          {!pinned && (
            <Button size="xs" variant="default" aria-label="Pin source" onClick={onPin}>
              ⇱ Pin
            </Button>
          )}
          <Button size="xs" variant="default" aria-label="Close source" onClick={onClose}>
            ✕
          </Button>
        </Group>
      </Group>

      {source.tombstoned && (
        <Text role="note" className={styles.sourceTombstoned}>
          This reference has been removed.
        </Text>
      )}
      {source.stance && (
        <Text span className={styles.sourceStance}>
          {source.stance}
        </Text>
      )}
      {source.claim && (
        <Text size="sm" c="dimmed">
          <strong>Claim:</strong> {source.claim}
        </Text>
      )}

      {source.no_text ? (
        <Text c="dimmed" size="sm">
          The source text isn’t available yet.
        </Text>
      ) : (
        <Box className={styles.sourceBody}>
          {validHighlight ? (
            <>
              {text.slice(0, validHighlight[0])}
              <mark>{text.slice(validHighlight[0], validHighlight[1])}</mark>
              {text.slice(validHighlight[1])}
            </>
          ) : (
            text
          )}
        </Box>
      )}

      {source.note && (
        <Text c="dimmed" size="sm">
          {source.note}
        </Text>
      )}
    </Box>
  );
}
