import { useEffect, useRef, useState } from "react";
import { useReferences } from "../api/citations";
import { ApiError } from "../api/client";
import { useDeleteFile, useFiles, useUploadFile } from "../api/files";
import {
  useDeleteReference,
  useFetchReference,
  useReprocessReference,
  useToggleWaiver,
  useUpdateKind,
  useUploadReference,
} from "../api/references";
import { MetadataModal } from "./MetadataModal";

const KINDS = ["paper", "prior_proposal", "analysis", "data"];

function rowLabel(r: { title: string; citekey?: string | null; filename: string }): string {
  return r.title || r.citekey || r.filename;
}

// Bounded-concurrency + 429-backoff upload queue (ports the Jinja app.js behavior,
// not the code) so a multi-file drop can't hammer the ingest limiter.
// The in-flight counter is a REF (not state) so pump() reads the live value, not
// a stale render closure; `uploading` state only drives the disabled affordance.
function useUploadQueue(id: string) {
  const upload = useUploadReference(id);
  const activeRef = useRef(0);
  const [uploading, setUploading] = useState(false);
  const MAX = 2;

  function enqueue(files: File[], kind: string) {
    const q = [...files];
    const settle = () => {
      activeRef.current -= 1;
      if (activeRef.current === 0 && q.length === 0) setUploading(false);
      pump();
    };
    const pump = () => {
      while (activeRef.current < MAX && q.length > 0) {
        const file = q.shift()!;
        activeRef.current += 1;
        setUploading(true);
        // mutateAsync gives each concurrent worker its OWN awaited settle path.
        // A single mutation observer's per-call mutate() callbacks are unreliable
        // under concurrency (TanStack reassigns the observer to the newest call),
        // which would skip retries and leak activeRef.
        const run = (attempt: number) =>
          upload
            .mutateAsync({ file, kind, autoClassify: true })
            .then(() => settle())
            .catch((err) => {
              if (err instanceof ApiError && err.code === "too_many_requests" && attempt < 4) {
                window.setTimeout(() => run(attempt + 1), 2000 * (attempt + 1));
                return; // keep the slot held during backoff; do NOT settle
              }
              settle();
            });
        run(0);
      }
    };
    pump();
  }
  return { enqueue, isUploading: uploading || upload.isPending };
}

export function LibraryPanel({ id }: { id: string }) {
  const refsQ = useReferences(id);
  const updateKind = useUpdateKind(id);
  const uploadOne = useUploadReference(id);
  const queue = useUploadQueue(id);
  const fetchRef = useFetchReference(id);
  const toggleWaiver = useToggleWaiver(id);
  const reprocess = useReprocessReference(id);
  const del = useDeleteReference(id);
  const files = useFiles(id);
  const uploadFile = useUploadFile(id);
  const deleteFile = useDeleteFile(id);
  const fileRows = Array.isArray(files.data) ? files.data : [];
  const [pickerKind, setPickerKind] = useState("paper");
  const [fetchInput, setFetchInput] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const refs = Array.isArray(refsQ.data) ? refsQ.data : [];

  // Show "Already processing." when reprocess fails with a conflict.
  useEffect(() => {
    if (
      reprocess.isError &&
      reprocess.error instanceof ApiError &&
      reprocess.error.code === "conflict"
    ) {
      setNote("Already processing.");
    }
  }, [reprocess.isError, reprocess.error]);

  function onDelete(refId: string) {
    if (!window.confirm("Delete this reference?")) return;
    del.mutate(refId, {
      onSuccess: (res) =>
        setNote(res.outcome === "tombstoned" ? "Retained (still cited)." : "Deleted."),
    });
  }

  if (refsQ.isError) return <p className="library-msg">Could not load references.</p>;
  if (refsQ.isLoading) return <p className="library-msg">Loading…</p>;

  return (
    <div className="library-panel">
      <section aria-label="Add references" className="library-add">
        <input
          aria-label="Add reference file"
          type="file"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadOne.mutate({ file: f, kind: pickerKind, autoClassify: false });
          }}
        />
        <select aria-label="New reference kind" value={pickerKind} onChange={(e) => setPickerKind(e.target.value)}>
          {KINDS.map((k) => (
            <option key={k} value={k}>{k}</option>
          ))}
        </select>
        <div
          className="library-dropzone"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            queue.enqueue(Array.from(e.dataTransfer.files), "paper");
          }}
        >
          Drop PDFs here
        </div>
        <div className="library-fetch">
          <input
            aria-label="Fetch by DOI / PMID / arXiv / URL"
            value={fetchInput}
            onChange={(e) => setFetchInput(e.target.value)}
          />
          <button onClick={() => fetchRef.mutate({ input: fetchInput, kind: pickerKind })}>Fetch</button>
        </div>
      </section>

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
                <button onClick={() => setEditing(r.id)}>Edit</button>
                <button
                  onClick={() =>
                    toggleWaiver.mutate({ referenceId: r.id, waiver: !r.metadata_waiver })
                  }
                >
                  {r.metadata_waiver ? "Remove waiver" : "Waiver"}
                </button>
                {r.text_status === "failed" && (
                  <button onClick={() => reprocess.mutate(r.id)}>
                    Reprocess
                  </button>
                )}
                <button onClick={() => onDelete(r.id)}>Delete</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Files">
        <h3 className="library-section-title">Files</h3>
        <input
          aria-label="Add image file"
          type="file"
          accept="image/png,image/jpeg,image/webp,image/gif"
          disabled={uploadFile.isPending}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadFile.mutate(f);
            e.target.value = "";
          }}
        />
        {files.isError ? (
          <p className="library-msg">Could not load files.</p>
        ) : files.isLoading ? (
          <p className="library-msg">Loading…</p>
        ) : fileRows.length === 0 ? (
          <p className="library-msg">No files yet.</p>
        ) : (
          <ul className="library-file-list">
            {fileRows.map((f) => (
              <li key={f.id} className="library-file-row">
                <img
                  className="library-thumb"
                  src={`/workspaces/${id}/files/${f.id}`}
                  alt={f.filename}
                />
                <span className="library-file-name">{f.filename}</span>
                <button
                  aria-label={`Delete ${f.filename}`}
                  disabled={deleteFile.isPending}
                  onClick={() => {
                    if (window.confirm(`Delete ${f.filename}?`)) deleteFile.mutate(f.id);
                  }}
                >
                  delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {editing && (
        <MetadataModal
          id={id}
          referenceId={editing}
          opened
          onClose={() => setEditing(null)}
        />
      )}
      {note && <p className="library-note">{note}</p>}
    </div>
  );
}
