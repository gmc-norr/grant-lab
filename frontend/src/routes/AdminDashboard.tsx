import { useState } from "react";
import { Link } from "react-router-dom";

import {
  useAdminDashboard,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "../api/adminDashboard";
import styles from "./AdminDashboard.module.css";

// Ported verbatim from admin.html _ok_msgs / _err_msgs (admin.html:23-31).
const OK_FETCH = "Fetch triggered — the source will refresh shortly.";
const OK_DIGEST = "Weekly digest triggered — it will send shortly.";
const OK_REENABLE = "User re-enabled for the weekly digest.";
const OK_RERANK = "Re-ranking enqueued for all active profiles.";
const ERR_FETCH = "Couldn't trigger the fetch — the scheduler or this source's job is unavailable.";
const ERR_DIGEST = "Digest isn't configured (no email backend), so it can't be sent.";

type Banner = { kind: "ok" | "warn"; text: string } | null;

const fmtDt = (s: string | null) => (s ? new Date(s).toLocaleString() : null);

export function AdminDashboard() {
  const { data, isLoading, isError } = useAdminDashboard();
  const [banner, setBanner] = useState<Banner>(null);
  const rerank = useRerank();
  const sendDigest = useSendDigest();
  const reenable = useReenableDigestUser();
  const fetchSource = useFetchSource();

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the admin dashboard.</p></main>;

  const d = data;

  const onFetch = (slug: string, name: string) =>
    fetchSource.mutate({ slug }, {
      onSuccess: (r) =>
        setBanner(r.triggered
          ? { kind: "ok", text: `${OK_FETCH} (${name})` }
          : { kind: "warn", text: `${ERR_FETCH} (${name})` }),
    });

  const onRerank = () => {
    if (!window.confirm(
      `This enqueues up to ~${d.rerank_estimate} ranking jobs and may take a while. Continue?`
    )) return;
    rerank.mutate(undefined, { onSuccess: () => setBanner({ kind: "ok", text: OK_RERANK }) });
  };

  const onSendDigest = () =>
    sendDigest.mutate(undefined, {
      onSuccess: (r) =>
        setBanner(r.triggered ? { kind: "ok", text: OK_DIGEST } : { kind: "warn", text: ERR_DIGEST }),
    });

  const onReenable = (userId: number) =>
    reenable.mutate({ user_id: userId },
      { onSuccess: () => setBanner({ kind: "ok", text: OK_REENABLE }) });

  return (
    <main className={styles.page}>
      <h1>Operational health</h1>
      {banner && (
        <div role={banner.kind === "ok" ? "status" : "alert"}
             className={banner.kind === "ok" ? styles.bannerOk : styles.bannerWarn}>
          {banner.text}
        </div>
      )}

      <section className={styles.card}>
        <h2>Source health</h2>
        <table className={styles.table}>
          <thead><tr><th>Source</th><th>Last fetched</th><th>Calls</th><th>Actions</th></tr></thead>
          <tbody>
            {d.sources.map((s) => (
              <tr key={s.slug}>
                <td>{s.name} <code className={styles.muted}>{s.slug}</code></td>
                <td>{fmtDt(s.last_fetched_at) ?? <span className={styles.muted}>never</span>}</td>
                <td><strong>{s.call_count}</strong></td>
                <td>
                  <button type="button" className={styles.secondary} disabled={fetchSource.isPending}
                          onClick={() => onFetch(s.slug, s.name)}>
                    Fetch now
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className={styles.card}>
        <h2>Ranking queue</h2>
        <div className={styles.cards}>
          <div className={styles.summary}><span>Pending</span><strong>{d.queue_counts.pending}</strong></div>
          <div className={styles.summary}><span>Running</span><strong>{d.queue_counts.running}</strong></div>
          <div className={styles.summary}><span>Done</span><strong>{d.queue_counts.done}</strong></div>
          <div className={styles.summary}><span>Failed</span><strong>{d.queue_counts.failed}</strong></div>
        </div>
        <button type="button" className={styles.secondary} disabled={rerank.isPending} onClick={onRerank}>
          Re-rank everything (last {d.backfill_days} days × active profiles)
        </button>
        <p className={styles.muted}>
          Enqueues up to ~{d.rerank_estimate} ranking jobs ({d.active_profiles_count} active
          profiles × {d.recent_calls_count} recent calls; upper bound).
        </p>
      </section>

      {d.calibration_warning && (
        <div role="alert" className={styles.bannerWarn}>
          <strong>Embedding thresholds uncalibrated.</strong> {d.calibration_warning}
        </div>
      )}

      <section className={styles.card}>
        <h2>Embedding coverage</h2>
        <dl className={styles.dl}>
          <dt>Calls</dt>
          <dd><strong>{d.embedding_coverage.call_embedded}/{d.embedding_coverage.call_total} embedded</strong>{" "}
            <span className={styles.muted}>({d.embedding_coverage.call_stale} stale, model <code>{d.embedding_coverage.current_model}</code>)</span></dd>
          <dt>Profiles</dt>
          <dd><strong>{d.embedding_coverage.profile_embedded}/{d.embedding_coverage.profile_total} embedded</strong>{" "}
            <span className={styles.muted}>({d.embedding_coverage.profile_stale} stale, model <code>{d.embedding_coverage.current_model}</code>)</span></dd>
        </dl>
      </section>

      <section className={styles.card}>
        <h2>Digest (weekly)</h2>
        <dl className={styles.dl}>
          <dt>Last digest sent</dt>
          <dd>{fmtDt(d.digest.last_sent_at) ?? <span className={styles.muted}>No digests sent</span>}</dd>
          <dt>Runs this week</dt><dd>{d.digest.runs_this_week}</dd>
          <dt>Disabled users (3+ bounces)</dt><dd>{d.digest.disabled_users}</dd>
        </dl>
        {d.digest.disabled_user_rows.length > 0 ? (
          <ul className={styles.disabled}>
            {d.digest.disabled_user_rows.map((u) => (
              <li key={u.id}>
                {u.email} <span className={styles.muted}>disabled {fmtDt(u.disabled_at)}</span>
                <button type="button" className={styles.secondary} disabled={reenable.isPending}
                        onClick={() => onReenable(u.id)}>Re-enable</button>
              </li>
            ))}
          </ul>
        ) : <p className={styles.muted}>No disabled users.</p>}
        <button type="button" className={styles.secondary} disabled={sendDigest.isPending} onClick={onSendDigest}>
          Send digest now
        </button>
      </section>

      <section className={styles.card}>
        <h2>Ranking quality — Kendall's τ (last run)</h2>
        <p><Link to="/admin/golden-set">Manage golden set →</Link></p>
        {d.golden_set_report ? (
          <dl className={styles.dl}>
            <dt>Curated triples</dt><dd>{d.golden_set_report.n_fixture}</dd>
            <dt>τ (n={d.golden_set_report.n_compared})</dt>
            <dd>{d.golden_set_report.tau !== null ? <strong>{d.golden_set_report.tau.toFixed(3)}</strong> : "—"}</dd>
          </dl>
        ) : (
          <p className={styles.muted}>Golden-set evaluation hasn't run yet — happens Mondays at 06:00 Europe/Stockholm.</p>
        )}
      </section>
    </main>
  );
}
