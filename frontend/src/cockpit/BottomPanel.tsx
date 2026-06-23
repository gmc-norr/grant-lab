import { useProblems } from "../api/problems";
import { useInboxCount } from "../api/proposals";
import { ActivityPanel } from "./ActivityPanel";
import { InboxPanel } from "./InboxPanel";
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
        </div>
      )}
    </section>
  );
}
