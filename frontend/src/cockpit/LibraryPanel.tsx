import { useEffect, useRef, useState } from "react";
import { Anchor, Badge, Box, Button, Group, NativeSelect, Stack, Text, TextInput, Title } from "@mantine/core";
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

  if (refsQ.isError) return <Text c="dimmed" size="sm">Could not load references.</Text>;
  if (refsQ.isLoading) return <Text c="dimmed" size="sm">Loading…</Text>;

  return (
    <Stack gap="md">
      <section aria-label="Add references">
        <Stack gap="sm">
          <input
            aria-label="Add reference file"
            type="file"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) uploadOne.mutate({ file: f, kind: pickerKind, autoClassify: false });
            }}
          />
          <NativeSelect
            aria-label="New reference kind"
            value={pickerKind}
            onChange={(e) => setPickerKind(e.target.value)}
            data={KINDS}
          />
          <Box
            style={{ border: "1px dashed var(--app-border)", borderRadius: 6, padding: "0.75rem", textAlign: "center" }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              queue.enqueue(Array.from(e.dataTransfer.files), "paper");
            }}
          >
            Drop PDFs here
          </Box>
          <Group gap="sm">
            <TextInput
              aria-label="Fetch by DOI / PMID / arXiv / URL"
              value={fetchInput}
              onChange={(e) => setFetchInput(e.target.value)}
            />
            <Button onClick={() => fetchRef.mutate({ input: fetchInput, kind: pickerKind })}>Fetch</Button>
          </Group>
        </Stack>
      </section>

      <section aria-label="References">
        <Title order={4}>References</Title>
        {refs.length === 0 ? (
          <Text c="dimmed" size="sm">No references yet.</Text>
        ) : (
          <Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {refs.map((r) => (
              <Group component="li" key={r.id} gap="sm" wrap="wrap">
                <Text size="sm" fw={600}>{rowLabel(r)}</Text>
                <NativeSelect
                  aria-label={`Kind for ${rowLabel(r)}`}
                  value={r.kind}
                  disabled={updateKind.isPending}
                  onChange={(e) =>
                    updateKind.mutate({ referenceId: r.id, kind: e.target.value })
                  }
                  data={KINDS}
                />
                <Badge size="sm" variant="light" data-status={r.text_status}>{r.text_status}</Badge>
                <Badge size="sm" variant="light" data-meta={r.metadata_status}>{r.metadata_status}</Badge>
                <Anchor
                  href={`/api/workspaces/${id}/references/${r.id}/raw`}
                  target="_blank"
                  rel="noreferrer"
                  size="sm"
                >
                  download
                </Anchor>
                <Button size="xs" variant="default" onClick={() => setEditing(r.id)}>Edit</Button>
                <Button
                  size="xs"
                  variant="default"
                  onClick={() =>
                    toggleWaiver.mutate({ referenceId: r.id, waiver: !r.metadata_waiver })
                  }
                >
                  {r.metadata_waiver ? "Remove waiver" : "Waiver"}
                </Button>
                {r.text_status === "failed" && (
                  <Button size="xs" variant="default" onClick={() => reprocess.mutate(r.id)}>
                    Reprocess
                  </Button>
                )}
                <Button size="xs" variant="subtle" onClick={() => onDelete(r.id)}>Delete</Button>
              </Group>
            ))}
          </Stack>
        )}
      </section>

      <section aria-label="Files">
        <Title order={4}>Files</Title>
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
          <Text c="dimmed" size="sm">Could not load files.</Text>
        ) : files.isLoading ? (
          <Text c="dimmed" size="sm">Loading…</Text>
        ) : fileRows.length === 0 ? (
          <Text c="dimmed" size="sm">No files yet.</Text>
        ) : (
          <Stack component="ul" gap="xs" style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {fileRows.map((f) => (
              <Group component="li" key={f.id} gap="sm">
                <img
                  src={`/api/workspaces/${id}/files/${f.id}`}
                  alt={f.filename}
                  width={40}
                  height={40}
                />
                <Text size="sm">{f.filename}</Text>
                <Button
                  size="xs"
                  variant="subtle"
                  aria-label={`Delete ${f.filename}`}
                  disabled={deleteFile.isPending}
                  onClick={() => {
                    if (window.confirm(`Delete ${f.filename}?`)) deleteFile.mutate(f.id);
                  }}
                >
                  delete
                </Button>
              </Group>
            ))}
          </Stack>
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
      {note && <Text c="dimmed" size="sm">{note}</Text>}
    </Stack>
  );
}
