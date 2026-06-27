import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail, useHideCall, useUnhideCall } from "../api/calls";
import { DeadlineLocal } from "./Deadline";
import styles from "./CallDetail.module.css";

function budgetText(c: { budget_min: number | null; budget_max: number | null; currency: string | null }): string {
  if (c.budget_min == null && c.budget_max == null) return "—";
  const fmt = (n: number) => n.toLocaleString("en-US");
  const lo = c.budget_min != null ? fmt(c.budget_min) : "—";
  const hi = c.budget_max != null ? fmt(c.budget_max) : "—";
  return `${lo} – ${hi} ${c.currency ?? ""}`.trim();
}

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  const hide = useHideCall();
  const unhide = useUnhideCall();
  const [confirmHide, setConfirmHide] = useState(false);
  const [hiddenId, setHiddenId] = useState<number | null>(null);
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <p role="alert">{notFound ? "Call not found." : "Couldn't load this call."}</p>;
  }

  const { call, ranking, criteria, best_profile_id } = data;
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (best_profile_id != null) startParams.set("profile_id", String(best_profile_id));

  return (
    <main className={styles.detail}>
      <Link to="/" className={styles.backlink}>← All calls</Link>

      <header className={styles.header}>
        <h1>{call.title}</h1>
        <p className={styles.sub}>
          {call.source_name}
          {call.programme && <> · {call.programme}</>}
          {call.external_id && <> · <code>{call.external_id}</code></>}
        </p>
        <div className={styles.actions}>
          <Link to={`/cockpit/new?${startParams.toString()}`} className={styles.startButton}>
            Start drafting workspace
          </Link>
        </div>
      </header>

      {ranking && (
        <section aria-label="Ranking">
          <article className={styles.hero}>
            <div
              className={`${styles.figure} ${
                ranking.aggregate_score >= 70 ? styles.strong : ranking.aggregate_score > 0 ? styles.mid : ""
              }`}
              data-band={ranking.aggregate_score >= 70 ? "strong" : ranking.aggregate_score > 0 ? "mid" : "low"}
            >
              {ranking.aggregate_score}
            </div>
            <div className={styles.heroBody}>
              <strong>Aggregate score</strong>
              {ranking.aggregate_reason_label && (
                <span className={styles.gateTag}>{ranking.aggregate_reason_label}</span>
              )}
              <span className={styles.gateTag} title="LLM that produced this ranking">
                Analyzed by {ranking.model_label}
              </span>
              {ranking.rationale && !ranking.aggregate_reason_label && (
                <div className={styles.rationale}>{ranking.rationale}</div>
              )}
              {ranking.embed_similarity != null && (
                <div className={styles.muted}>Cosine similarity: {ranking.embed_similarity.toFixed(3)}</div>
              )}
              {ranking.escalated && ranking.escalated_from_score != null && (
                <div className={styles.muted}>
                  Escalated from {ranking.escalated_from_score} (analysis by {ranking.analysis_model_label}).
                </div>
              )}
            </div>
          </article>
        </section>
      )}

      {ranking && (ranking.summary || ranking.applicant_notes.length > 0) && (
        <section className={styles.section} aria-label="Deep analysis">
          <h2>
            Analyst notes <small className={styles.muted}>— {ranking.model_label}</small>
          </h2>
          {ranking.summary && <p>{ranking.summary}</p>}
          {ranking.applicant_notes.length > 0 && (
            <div className={styles.notes}>
              {ranking.applicant_notes.map((n, i) => (
                <article key={i} className={styles.note}>
                  <h3>{n.heading}</h3>
                  <p>{n.text}</p>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      <section className={styles.section} aria-label="Call details">
        <h2>Details</h2>
        <div className={styles.detailGrid}>
          <div className={styles.kv}>
            <span className={styles.k}>Published</span>
            <span className={styles.v}>{call.published_at ? call.published_at.slice(0, 10) : "—"}</span>
          </div>
          <div className={styles.kv}>
            <span className={styles.k}>Deadline</span>
            <span className={styles.v}>
              {call.deadline
                ? <DeadlineLocal localDate={call.deadline_local_date ?? null} isoFallback={call.deadline} />
                : "—"}
            </span>
          </div>
          <div className={styles.kv}>
            <span className={styles.k}>Budget</span>
            <span className={styles.v}>{budgetText(call)}</span>
          </div>
          <div className={styles.kv}>
            <span className={styles.k}>External link</span>
            <span className={styles.v}>
              <a href={call.url} target="_blank" rel="noopener noreferrer">Open at source →</a>
            </span>
          </div>
        </div>
        {call.keywords.length > 0 && (
          <p className={styles.keywords}>
            <small className={styles.muted}>Keywords:</small>
            {call.keywords.map((kw) => (
              <span key={kw} className={styles.chip}>{kw}</span>
            ))}
          </p>
        )}
      </section>

      {call.summary && (
        <section className={styles.section}>
          <h2>Summary</h2>
          <p>{call.summary}</p>
        </section>
      )}

      {criteria.length > 0 && (
        <section className={styles.section}>
          <h2>Criterion breakdown</h2>
          <div className={styles.criteria}>
            {criteria.map((c) => (
              <div key={c.criterion} className={styles.card}>
                <div className={styles.cardHead}>
                  <span>{c.label}</span>
                  <strong>{c.is_topic_fit ? `${c.rating}/5` : c.rating}</strong>
                </div>
                {c.detail && <div className={styles.rationale}>{c.detail}</div>}
                {c.rationale && (!c.detail || !c.detail.includes(c.rationale)) && (
                  <div className={styles.muted}>
                    {c.is_reason_code ? <>Reason code: <code>{c.rationale}</code></> : c.rationale}
                  </div>
                )}
                {c.quoted_evidence && <blockquote className={styles.quote}>"{c.quoted_evidence}"</blockquote>}
              </div>
            ))}
          </div>
        </section>
      )}

      <section className={styles.hideCall} aria-label="Hide this call">
        {hiddenId !== null ? (
          // Undo affordance: call was just hidden; offer a one-click undo before
          // navigating away.  A full "Hidden calls" view is a follow-up.
          <p className={styles.muted}>
            Call hidden.{" "}
            <button
              type="button"
              className={styles.undoButton}
              disabled={unhide.isPending}
              onClick={async () => {
                await unhide.mutateAsync(hiddenId);
                setHiddenId(null);
              }}
            >
              Undo
            </button>
            {" "}
            <button
              type="button"
              onClick={() => navigate("/")}
            >
              Back to all calls
            </button>
          </p>
        ) : confirmHide ? (
          // Confirm step — prevents accidental one-click hide.
          <p className={styles.muted}>
            Hide this call from your feed?{" "}
            <button
              type="button"
              className={styles.hideButton}
              disabled={hide.isPending}
              onClick={async () => {
                await hide.mutateAsync(call.id);
                setConfirmHide(false);
                setHiddenId(call.id);
              }}
            >
              Yes, hide it
            </button>
            {" "}
            <button type="button" onClick={() => setConfirmHide(false)}>
              Cancel
            </button>
          </p>
        ) : (
          <>
            <button
              type="button"
              className={styles.hideButton}
              onClick={() => setConfirmHide(true)}
            >
              Hide this call
            </button>
            <small className={styles.muted}>Removes it from your feed. Other users are unaffected.</small>
          </>
        )}
      </section>
    </main>
  );
}
