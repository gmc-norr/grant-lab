import { useCitationAudit } from "../api/audit";
import { useAnnotations } from "../api/annotations";
import { useProblems } from "../api/problems";
import { useInboxCount } from "../api/proposals";
import { ActivityPanel } from "./ActivityPanel";
import { CitationsPanel } from "./CitationsPanel";
import { ClaimsPanel } from "./ClaimsPanel";
import { InboxPanel } from "./InboxPanel";
import { LibraryPanel } from "./LibraryPanel";
import type { BottomTab, OnNavigate } from "./nav";
import { ProblemsPanel } from "./ProblemsPanel";

export function BottomPanel({
  id,
  activeTab,
  onTab,
  collapsed,
  onToggleCollapsed,
  onNavigate,
}: {
  id: string;
  activeTab: BottomTab;
  onTab: (t: BottomTab) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  onNavigate: OnNavigate;
}) {
  const inboxCount = useInboxCount(id);
  const problems = useProblems(id);
  const problemsCount =
    (problems.data?.counts?.error ?? 0) + (problems.data?.counts?.warning ?? 0);
  const audit = useCitationAudit(id);
  const auditCount = audit.data?.error_count ?? 0;
  const annotations = useAnnotations(id);
  const claimsCount =
    annotations.data?.sections.reduce((n, s) => n + (s.findings?.length ?? 0), 0) ?? 0;

  return (
    <section className="bottom-panel" aria-label="Workspace panel">
      <div className="bottom-panel-bar">
        <div role="tablist" aria-label="Panel" className="bottom-panel-tabs">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "inbox"}
            onClick={() => onTab("inbox")}
          >
            Inbox {inboxCount}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "problems"}
            onClick={() => onTab("problems")}
          >
            Problems {problemsCount}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "activity"}
            onClick={() => onTab("activity")}
          >
            Activity
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "library"}
            onClick={() => onTab("library")}
          >
            Library
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "citations"}
            onClick={() => onTab("citations")}
          >
            Citations {auditCount}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === "claims"}
            onClick={() => onTab("claims")}
          >
            Claims {claimsCount}
          </button>
        </div>
        <button
          type="button"
          className="bottom-panel-toggle"
          aria-expanded={!collapsed}
          onClick={onToggleCollapsed}
        >
          {collapsed ? "Expand panel" : "Collapse panel"}
        </button>
      </div>
      {!collapsed && (
        <div className="bottom-panel-body">
          {activeTab === "inbox" && <InboxPanel id={id} count={inboxCount} />}
          {activeTab === "problems" && <ProblemsPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "activity" && <ActivityPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "library" && <LibraryPanel id={id} />}
          {activeTab === "citations" && <CitationsPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "claims" && <ClaimsPanel id={id} />}
        </div>
      )}
    </section>
  );
}
