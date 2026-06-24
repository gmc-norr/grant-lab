import { Link, useSearchParams } from "react-router-dom";

import type { components } from "../api/schema.gen";
import { useWorkspaces } from "../api/workspace";
import { Deadline } from "./Deadline";
import styles from "./WorkspaceList.module.css";

type WsItem = components["schemas"]["WorkspaceListItemOut"];

const STATUS_LABEL: Record<string, string> = {
  drafting: "Drafting",
  submitted: "Submitted",
  awarded: "Awarded",
  rejected: "Rejected",
  abandoned: "Abandoned",
};

function WorkspaceRow({ ws }: { ws: WsItem }) {
  // Setup-phase workspaces aren't cockpit-ready (no repo yet); the setup + rubric
  // flow is still Jinja (Slice E) — full-page hand-off via <a>. Active workspaces
  // open the React cockpit.
  const setup = ws.current_phase === "setup";
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        {setup ? (
          <a className={styles.title} href={`/workspaces/${ws.id}`}>
            {ws.title}
          </a>
        ) : (
          <Link className={styles.title} to={`/cockpit/${ws.id}`}>
            {ws.title}
          </Link>
        )}
        <div className={styles.meta}>
          <span className={styles.status}>{STATUS_LABEL[ws.status] ?? ws.status}</span>
          {ws.linked_call_title && (
            <>
              <span aria-hidden> · </span>
              <span>{ws.linked_call_title}</span>
            </>
          )}
          {ws.deadline && (
            <>
              <span aria-hidden> · </span>
              <Deadline iso={ws.deadline} />
            </>
          )}
          <span aria-hidden> · </span>
          {/* updated_at is an ISO string; slice to YYYY-MM-DD for a stable label. */}
          <span className={styles.activity}>Updated {ws.updated_at.slice(0, 10)}</span>
          {!ws.is_owner && <span className={styles.badge}>Shared</span>}
          {setup && <span className={styles.setup}>Setting up</span>}
        </div>
      </div>
    </li>
  );
}

export function WorkspaceList() {
  const [sp] = useSearchParams();
  const status = sp.get("status");
  const { data, isLoading, isError } = useWorkspaces(status);

  if (isLoading) return <p>Loading…</p>;
  if (isError) return <p role="alert">Couldn&apos;t load workspaces.</p>;

  const items = data ?? [];
  return (
    <main>
      <div className={styles.header}>
        <h1 className={styles.h1}>Workspaces</h1>
        <Link className={styles.newBtn} to="/">
          New workspace
        </Link>
      </div>
      {items.length === 0 ? (
        <p className={styles.empty}>
          No workspaces yet — <Link to="/">pick a call to start drafting</Link>.
        </p>
      ) : (
        <ul className={styles.list}>
          {items.map((ws) => (
            <WorkspaceRow key={ws.id} ws={ws} />
          ))}
        </ul>
      )}
    </main>
  );
}
