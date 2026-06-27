import { Link } from "react-router-dom";

import { useAdminDrafting } from "../api/adminDashboard";
import styles from "./AdminDrafting.module.css";

const fmtDt = (s: string) => new Date(s).toLocaleString();

export function AdminDrafting() {
  const { data, isLoading, isError } = useAdminDrafting();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the drafting panel.</p></main>;
  const d = data;
  const cap = d.global_cap;
  return (
    <main className={styles.page}>
      <h1>Drafting (B7/B8) panel</h1>

      <section className={styles.card}>
        <h2>Kill switch</h2>
        <p>{d.drafting_enabled ? <strong>Drafting enabled</strong> : <strong>Drafting disabled</strong>}</p>
        <p className={styles.muted}>Toggle via env <code>DRAFTING_ENABLED=true|false</code> and restart. UI toggling is a backlog item.</p>
      </section>

      <section className={styles.card}>
        <h2>Global daily cap (today, Europe/Stockholm)</h2>
        <table className={styles.table}>
          <thead><tr><th>Dimension</th><th>Used / Cap</th><th>Status</th></tr></thead>
          <tbody>
            <tr><td>Input tokens</td><td>{cap.used_in} / {cap.cap_in}</td>
              <td>{cap.reason === "input_cap_exceeded" ? <strong>EXCEEDED</strong> : "OK"}</td></tr>
            <tr><td>Output tokens</td><td>{cap.used_out} / {cap.cap_out}</td>
              <td>{cap.reason === "output_cap_exceeded" ? <strong>EXCEEDED</strong> : "OK"}</td></tr>
          </tbody>
        </table>
        <p>{cap.allowed ? "Overall status: OK" : <strong>Overall status: EXCEEDED ({cap.reason})</strong>}</p>
      </section>

      <section className={styles.card}>
        <h2>Per-workspace usage today</h2>
        <table className={styles.table}>
          <thead><tr><th>Title</th><th>Owner</th><th>Input</th><th>Output</th></tr></thead>
          <tbody>
            {d.per_workspace.length === 0 ? (
              <tr><td colSpan={4} className={styles.muted}>No workspaces.</td></tr>
            ) : d.per_workspace.map((w) => (
              <tr key={w.id}>
                <td><Link to={`/cockpit/${w.id}`}>{w.title}</Link></td>
                <td>{w.owner_email}</td><td>{w.used_in}</td><td>{w.used_out}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className={styles.card}>
        <h2>Recent failed / cancelled turns (last 50)</h2>
        {d.recent_events.length === 0 ? (
          <p className={styles.muted}>No recent failures.</p>
        ) : (
          <table className={styles.table}>
            <thead><tr><th>When</th><th>Workspace</th><th>Kind</th><th>Detail</th></tr></thead>
            <tbody>
              {d.recent_events.map((e, i) => (
                <tr key={i}>
                  <td>{fmtDt(e.created_at)}</td><td>{e.workspace_id}</td>
                  <td><code>{e.kind}</code></td>
                  <td><pre className={styles.pre}>{JSON.stringify(e.payload, null, 2)}</pre></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
