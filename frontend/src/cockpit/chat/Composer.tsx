import { useState } from "react";

import { useUploadFile } from "../../api/files";
import { useSubmitTurn } from "../../api/turns";
import styles from "./CockpitChat.module.css";

type Ref = { id: string; citekey?: string | null; title: string; text_status?: string };

export function Composer({ id, refs, disabled, onSubmitted }: { id: string; refs: Ref[]; disabled: boolean; onSubmitted?: (jobId: string) => void }) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [images, setImages] = useState<{ attachment_id: string; filename: string }[]>([]);
  const submit = useSubmitTurn(id);
  const uploadFile = useUploadFile(id);
  const citable = refs.filter((r) => r.text_status === "done");
  const busy = disabled || submit.isPending;
  // Count an in-flight upload toward the cap: it will become a committed image
  // on onSuccess, so a pending upload already occupies one of the 5 slots.
  // Without this, the ref-<select> stays enabled during upload and lets the
  // user add refs that push the total past 5 → backend 400 on send.
  const inclusions = picked.length + images.length + (uploadFile.isPending ? 1 : 0);
  const atCap = inclusions >= 5;

  function onPickImage(file: File) {
    if (atCap) return;
    uploadFile.mutate(file, {
      onSuccess: (r) =>
        setImages((prev) =>
          prev.some((x) => x.attachment_id === r.attachment_id)
            ? prev
            : [...prev, { attachment_id: r.attachment_id, filename: r.filename }],
        ),
    });
  }

  const send = () => {
    if (!text.trim() || busy || uploadFile.isPending) return;
    submit.mutate(
      { content: text, reference_ids: picked, attachment_ids: images.map((i) => i.attachment_id) },
      {
        onSuccess: (result) => {
          setText("");
          setPicked([]);
          setImages([]);
          onSubmitted?.(result.job_id);
        },
      },
    );
  };

  return (
    <div className={styles.composer}>
      {(picked.length > 0 || images.length > 0) && (
        <div className={styles.picked}>
          {picked.map((pid) => {
            const r = citable.find((x) => x.id === pid);
            return (
              <span key={pid} className={styles.refChip}>
                {r?.citekey ?? r?.title ?? "ref"}
              </span>
            );
          })}
          {images.map((im) => (
            <span key={im.attachment_id} className={styles.imgChip}>
              {im.filename}
              <button
                aria-label={`Remove ${im.filename}`}
                onClick={() => setImages((prev) => prev.filter((x) => x.attachment_id !== im.attachment_id))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <textarea
        className={styles.textarea}
        placeholder="Message the agent…"
        value={text}
        disabled={busy}
        onChange={(e) => { setText(e.target.value); }}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
        }}
      />
      <div className={styles.affordances}>
        {citable.length > 0 && (
          <select
            className={styles.refSelect}
            value=""
            disabled={busy || atCap}
            onChange={(e) => {
              if (e.target.value && !picked.includes(e.target.value))
                setPicked([...picked, e.target.value]);
            }}
          >
            <option value="">📚 Add reference…</option>
            {citable.map((r) => (
              <option key={r.id} value={r.id}>
                {r.citekey ?? r.title}
              </option>
            ))}
          </select>
        )}
        {atCap && <span className={styles.hint}>Up to 5 inclusions per turn</span>}
        <label className={styles.attach}>
          <input
            aria-label="Attach image"
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif"
            disabled={busy || atCap || uploadFile.isPending}
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) onPickImage(f);
              e.target.value = "";
            }}
          />
          <span>📎 Image</span>
        </label>
        <button
          type="button"
          className={styles.send}
          disabled={busy || uploadFile.isPending || !text.trim()}
          onClick={send}
        >
          Send
        </button>
      </div>
      {submit.isError && (
        <p className={styles.error}>{(submit.error as Error)?.message ?? "Couldn't send."}</p>
      )}
    </div>
  );
}
