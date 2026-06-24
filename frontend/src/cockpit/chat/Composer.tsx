import { useState } from "react";

import { useSubmitTurn } from "../../api/turns";
import styles from "./CockpitChat.module.css";

type Ref = { id: string; citekey?: string | null; title: string; text_status?: string };

export function Composer({ id, refs, disabled, onSubmitted }: { id: string; refs: Ref[]; disabled: boolean; onSubmitted?: (jobId: string) => void }) {
  const [text, setText] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const submit = useSubmitTurn(id);
  const citable = refs.filter((r) => r.text_status === "done");
  const busy = disabled || submit.isPending;

  const send = () => {
    if (!text.trim() || busy) return;
    submit.mutate(
      { content: text, reference_ids: picked },
      {
        onSuccess: (result) => {
          setText("");
          setPicked([]);
          onSubmitted?.(result.job_id);
        },
      },
    );
  };

  return (
    <div className={styles.composer}>
      {picked.length > 0 && (
        <div className={styles.picked}>
          {picked.map((pid) => {
            const r = citable.find((x) => x.id === pid);
            return (
              <span key={pid} className={styles.refChip}>
                {r?.citekey ?? r?.title ?? "ref"}
              </span>
            );
          })}
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
            disabled={busy}
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
        <span className={styles.deferred} title="Image attachments arrive with the Files slice">
          📎 Image
        </span>
        <button
          type="button"
          className={styles.send}
          disabled={busy || !text.trim()}
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
