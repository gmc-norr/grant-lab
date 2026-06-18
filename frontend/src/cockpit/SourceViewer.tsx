import type { components } from "../api/schema.gen";

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
  return (
    <aside className="source-viewer" aria-label="Cited source">
      <header className="source-viewer-head">
        <span className="source-viewer-title">{source.source_line}</span>
        {source.total > 0 && (
          <span className="source-viewer-pos">
            {source.index} of {source.total}
          </span>
        )}
        <div className="source-viewer-actions">
          <button
            type="button"
            aria-label="Previous citation"
            disabled={!source.prev_ev}
            onClick={() => source.prev_ev && onNavigate(source.prev_ev)}
          >
            ‹
          </button>
          <button
            type="button"
            aria-label="Next citation"
            disabled={!source.next_ev}
            onClick={() => source.next_ev && onNavigate(source.next_ev)}
          >
            ›
          </button>
          {!pinned && (
            <button type="button" aria-label="Pin source" onClick={onPin}>
              ⇱ Pin
            </button>
          )}
          <button type="button" aria-label="Close source" onClick={onClose}>
            ✕
          </button>
        </div>
      </header>

      {source.tombstoned && (
        <p className="source-viewer-tombstoned" role="note">
          This reference has been removed.
        </p>
      )}
      {source.stance && (
        <p className="source-viewer-meta">
          <span className={`source-viewer-stance stance-${source.stance}`}>{source.stance}</span>
        </p>
      )}
      {source.claim && (
        <p className="source-viewer-claim">
          <strong>Claim:</strong> {source.claim}
        </p>
      )}

      {source.no_text ? (
        <p className="source-viewer-empty">The source text isn’t available yet.</p>
      ) : (
        <div className="source-viewer-body">
          {highlight ? (
            <>
              {text.slice(0, highlight[0])}
              <mark>{text.slice(highlight[0], highlight[1])}</mark>
              {text.slice(highlight[1])}
            </>
          ) : (
            text
          )}
        </div>
      )}

      {source.note && <p className="source-viewer-note">{source.note}</p>}
    </aside>
  );
}
