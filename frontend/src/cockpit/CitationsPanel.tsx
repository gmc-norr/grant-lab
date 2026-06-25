import { useEffect } from "react";

import { type AuditIssue, useCitationAudit, useDeepCheck } from "../api/audit";
import { useCockpitMeta } from "../api/workspace";
import type { OnNavigate } from "./nav";

export function CitationsPanel({ id, onNavigate }: { id: string; onNavigate: OnNavigate }) {
  const audit = useCitationAudit(id);
  const deep = useDeepCheck(id);
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";

  // Drop any prior deep-check result when the workspace changes, so stale deep data
  // can never supersede a different workspace's deterministic baseline (Codex finding 3).
  useEffect(() => {
    deep.reset();
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps -- reset is stable; intentional id-only

  if (audit.isLoading) return <p aria-busy="true">Checking citations…</p>;
  if (audit.isError || !audit.data)
    return <p role="alert">Could not run the citation audit.</p>;
  if (!audit.data.draft_present)
    return <p className="citations-empty">No draft yet — citations appear once drafting begins.</p>;

  // Deep-check results (when present) supersede the deterministic baseline. Render errors
  // before warnings (spec §5.1) — the backend returns them interleaved per row. Array.sort
  // is stable, so within a severity the backend order is preserved.
  const RANK: Record<string, number> = { error: 0, warning: 1 };
  const issues: AuditIssue[] = [...(deep.data?.issues ?? audit.data.issues)].sort(
    (a, b) => (RANK[a.severity] ?? 9) - (RANK[b.severity] ?? 9),
  );

  return (
    <div className="citations-panel">
      <div className="citations-toolbar">
        <button
          type="button"
          onClick={() => {
            deep.reset(); // clear superseding deep-check data so Re-run shows the fresh baseline
            void audit.refetch();
          }}
        >
          Re-run
        </button>
        {isOwner && (
          <button type="button" onClick={() => deep.mutate()} disabled={deep.isPending}>
            {deep.isPending ? "Deep checking…" : "Deep check"}
          </button>
        )}
        {deep.data && (
          <span className="citations-summary">
            Checked {deep.data.checked} · {deep.data.error_count} error
            {deep.data.error_count === 1 ? "" : "s"} · {deep.data.warning_count} warning
            {deep.data.warning_count === 1 ? "" : "s"}
          </span>
        )}
      </div>
      {deep.data?.cap_exceeded && (
        <p className="citations-cap" role="status">
          Deep check hit the daily AI budget — some citations may not have been judged. Try
          again tomorrow.
        </p>
      )}
      {deep.data && !deep.data.semantic_available && (
        <p className="citations-note">
          Semantic checking is unavailable; showing deterministic results only.
        </p>
      )}
      {issues.length === 0 ? (
        <p className="citations-clean">No citation issues found.</p>
      ) : (
        <ul className="citations-issues">
          {issues.map((iss, i) => (
            <li key={i} className={`severity-${iss.severity}`}>
              <span className="issue-type">{iss.type}</span>
              {iss.citekey && <code>{iss.citekey}</code>}
              {iss.location && <span className="issue-loc">{iss.location}</span>}
              <span className="issue-detail">{iss.detail}</span>
              {iss.evidence_id && (
                <button
                  type="button"
                  onClick={() =>
                    onNavigate({ top_tab: "draft", evidence_id: iss.evidence_id ?? undefined })
                  }
                >
                  View source
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
