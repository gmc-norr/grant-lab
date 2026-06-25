import { type FormEvent, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useCreateWorkspace } from "../api/workspace";
import styles from "./WorkspaceCreate.module.css";

export function WorkspaceCreate() {
  const [sp] = useSearchParams();
  const callIdRaw = sp.get("call_id");
  const callId = callIdRaw ? Number(callIdRaw) : null;
  const callTitle = sp.get("call_title") ?? "";
  const profileIdRaw = sp.get("profile_id");
  const profileId = profileIdRaw ? Number(profileIdRaw) : null;

  const [title, setTitle] = useState(callTitle);
  const [deadline, setDeadline] = useState("");
  const create = useCreateWorkspace();
  const navigate = useNavigate();

  // From-call only: without a call there is nothing to draft against (Phase-0
  // requires a call). Guard a direct hit / refresh-without-params.
  if (callId === null || Number.isNaN(callId)) {
    return (
      <main className={styles.guard} role="alert">
        <h1>Start drafting</h1>
        <p>Pick a call from the dashboard to start drafting.</p>
        <Link to="/">← Back to calls</Link>
      </main>
    );
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    const out = await create.mutateAsync({
      call_id: callId as number,
      title,
      deadline: deadline || undefined,
      profile_id: profileId,
    });
    // Setup + rubric now live in the React cockpit (Slice E).
    navigate(`/cockpit/${out.id}`);
  }

  return (
    <main className={styles.wrap}>
      <h1>New workspace</h1>
      <p className={styles.context}>Drafting for: {callTitle || `call #${callId}`}</p>
      <form onSubmit={onSubmit} className={styles.form}>
        <label className={styles.label}>
          Title
          <input
            className={styles.input}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            maxLength={500}
          />
        </label>
        <label className={styles.label}>
          Deadline (optional)
          <input
            className={styles.input}
            type="datetime-local"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
          />
        </label>
        <button
          className={styles.submit}
          type="submit"
          disabled={create.isPending || !title.trim()}
        >
          {create.isPending ? "Creating…" : "Start drafting"}
        </button>
        {create.isError && (
          <p role="alert" className={styles.error}>
            {create.error instanceof Error
              ? create.error.message
              : "Couldn't create the workspace."}
          </p>
        )}
      </form>
    </main>
  );
}
