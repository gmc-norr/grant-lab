import { useReferences } from "../api/citations";
import { useUpdateKind } from "../api/references";

const KINDS = ["paper", "prior_proposal", "analysis", "data"];

function rowLabel(r: { title: string; citekey?: string | null; filename: string }): string {
  return r.title || r.citekey || r.filename;
}

export function LibraryPanel({ id }: { id: string }) {
  const refsQ = useReferences(id);
  const updateKind = useUpdateKind(id);
  const refs = Array.isArray(refsQ.data) ? refsQ.data : [];

  if (refsQ.isError) return <p className="library-msg">Could not load references.</p>;
  if (refsQ.isLoading) return <p className="library-msg">Loading…</p>;

  return (
    <div className="library-panel">
      <section aria-label="References">
        <h3 className="library-section-title">References</h3>
        {refs.length === 0 ? (
          <p className="library-msg">No references yet.</p>
        ) : (
          <ul className="library-list">
            {refs.map((r) => (
              <li key={r.id} className="library-row">
                <span className="library-row-label">{rowLabel(r)}</span>
                <select
                  aria-label={`Kind for ${rowLabel(r)}`}
                  value={r.kind}
                  disabled={updateKind.isPending}
                  onChange={(e) =>
                    updateKind.mutate({ referenceId: r.id, kind: e.target.value })
                  }
                >
                  {KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
                <span className="library-badge" data-status={r.text_status}>
                  {r.text_status}
                </span>
                <span className="library-badge" data-meta={r.metadata_status}>
                  {r.metadata_status}
                </span>
                <a
                  className="library-dl"
                  href={`/workspaces/${id}/references/${r.id}/raw`}
                  target="_blank"
                  rel="noreferrer"
                >
                  download
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
