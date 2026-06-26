import { useState } from "react";

import {
  type ClaimFinding,
  type ClaimSection,
  useAnnotations,
  useDismissAnnotation,
  useInsertAnnotation,
  useReanalyzeSection,
} from "../api/annotations";

const KIND_LABEL: Record<string, string> = {
  uncited_supported: "Uncited · supported",
  uncited_no_match: "Uncited · no match",
  cited_unsupported: "Cited · unsupported",
};

export function ClaimsPanel({ id }: { id: string }) {
  const ann = useAnnotations(id);
  const insert = useInsertAnnotation(id);
  const dismiss = useDismissAnnotation(id);
  const reanalyze = useReanalyzeSection(id);

  if (ann.isLoading) return <p aria-busy="true">Loading claims…</p>;
  if (ann.isError || !ann.data) return <p role="alert">Could not load claims.</p>;

  const isOwner = ann.data.is_owner;
  if (ann.data.sections.length === 0)
    return <p className="claims-empty">No draft yet — claims appear once drafting begins.</p>;

  return (
    <div className="claims-panel">
      {ann.data.sections.map((sec) => (
        <ClaimSectionView
          key={`${sec.section_sha}:${sec.occurrence}`}
          section={sec}
          isOwner={isOwner}
          onInsert={(fid) => insert.mutate(fid)}
          onDismiss={(fid, reason) => dismiss.mutate({ annotationId: fid, reason })}
          onReanalyze={() => reanalyze.mutate(sec.index)}
        />
      ))}
    </div>
  );
}

function ClaimSectionView({
  section,
  isOwner,
  onInsert,
  onDismiss,
  onReanalyze,
}: {
  section: ClaimSection;
  isOwner: boolean;
  onInsert: (findingId: number) => void;
  onDismiss: (findingId: number, reason: string) => void;
  onReanalyze: () => void;
}) {
  return (
    <section className="claims-section">
      <header className="claims-section-head">
        <h4>{section.heading || "(preamble)"}</h4>
        {section.job.status === "pending" && (
          <span className="claims-job" role="status">
            analyzing…
          </span>
        )}
        {section.job.status === "failed" && (
          <span className="claims-job claims-job-failed" role="status">
            analysis failed{section.job.error ? ` — ${section.job.error}` : ""}
          </span>
        )}
        {isOwner && (
          <button type="button" onClick={onReanalyze}>
            Reanalyze
          </button>
        )}
      </header>
      {section.findings.length === 0 ? (
        <p className="claims-none">No open findings</p>
      ) : (
        <ul className="claims-findings">
          {section.findings.map((f) => (
            <FindingRow
              key={f.id}
              finding={f}
              isOwner={isOwner}
              onInsert={() => onInsert(f.id)}
              onDismiss={(reason) => onDismiss(f.id, reason)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function FindingRow({
  finding,
  isOwner,
  onInsert,
  onDismiss,
}: {
  finding: ClaimFinding;
  isOwner: boolean;
  onInsert: () => void;
  onDismiss: (reason: string) => void;
}) {
  const [dismissing, setDismissing] = useState(false);
  const [reason, setReason] = useState("");

  return (
    <li className={`claims-finding kind-${finding.kind}`}>
      <span className="claims-kind">{KIND_LABEL[finding.kind] ?? finding.kind}</span>
      <p className="claims-claim">{finding.claim_text}</p>
      {finding.evidence_quote && <blockquote className="claims-quote">{finding.evidence_quote}</blockquote>}
      <span className="claims-ref">{finding.ref_label}</span>
      {isOwner && (
        <div className="claims-actions">
          {finding.can_insert && (
            <button type="button" onClick={onInsert}>
              Insert
            </button>
          )}
          {!dismissing ? (
            <button type="button" onClick={() => setDismissing(true)}>
              Dismiss
            </button>
          ) : (
            <span className="claims-dismiss-form">
              <input
                type="text"
                placeholder="Reason (required)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                type="button"
                aria-label="Confirm dismiss"
                disabled={reason.trim().length === 0}
                onClick={() => onDismiss(reason.trim())}
              >
                Confirm dismiss
              </button>
            </span>
          )}
        </div>
      )}
    </li>
  );
}
