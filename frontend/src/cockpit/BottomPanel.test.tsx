import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BottomPanel } from "./BottomPanel";

const h = vi.hoisted(() => ({
  count: 3,
  problems: { data: { problems: [], counts: { error: 2, warning: 1 }, scorecard_stale: [] } },
  audit: { data: { error_count: 2 } },
}));

vi.mock("../api/proposals", () => ({ useInboxCount: () => h.count }));
vi.mock("../api/problems", () => ({ useProblems: () => h.problems }));
vi.mock("../api/audit", () => ({ useCitationAudit: () => h.audit }));
vi.mock("../api/annotations", () => ({ useAnnotations: () => ({ data: { is_owner: true, sections: [] } }) }));
vi.mock("./ClaimsPanel", () => ({ ClaimsPanel: () => <div>claims-body</div> }));
vi.mock("./InboxPanel", () => ({ InboxPanel: () => <div>inbox-body</div> }));
vi.mock("./ProblemsPanel", () => ({ ProblemsPanel: () => <div>problems-body</div> }));
vi.mock("./ActivityPanel", () => ({ ActivityPanel: () => <div>activity-body</div> }));
vi.mock("./CitationsPanel", () => ({ CitationsPanel: () => <div>citations-body</div> }));

describe("BottomPanel", () => {
  it("renders badges and switches tabs", () => {
    const onTab = vi.fn();
    render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    // badges
    expect(screen.getByRole("tab", { name: /inbox 3/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /problems 3/i })).toBeInTheDocument(); // 2+1
    // active body
    expect(screen.getByText("inbox-body")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /problems 3/i }));
    expect(onTab).toHaveBeenCalledWith("problems");
  });

  it("hides the bodies when collapsed and toggles", () => {
    const onToggle = vi.fn();
    render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={vi.fn()}
        collapsed={true}
        onToggleCollapsed={onToggle}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.queryByText("inbox-body")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /expand panel/i }));
    expect(onToggle).toHaveBeenCalled();
  });

  it("exposes the Activity tab and shows its body when active", () => {
    const onTab = vi.fn();
    const { rerender } = render(
      <BottomPanel
        id="ws1"
        activeTab="inbox"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    const tab = screen.getByRole("tab", { name: /^activity$/i });
    expect(tab).toBeInTheDocument();
    fireEvent.click(tab);
    expect(onTab).toHaveBeenCalledWith("activity");
    rerender(
      <BottomPanel
        id="ws1"
        activeTab="activity"
        onTab={onTab}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.getByText("activity-body")).toBeInTheDocument();
  });

  it("exposes the Citations tab with an error-count badge", () => {
    render(
      <BottomPanel
        id="ws1"
        activeTab="citations"
        onTab={vi.fn()}
        collapsed={false}
        onToggleCollapsed={vi.fn()}
        onNavigate={vi.fn()}
      />,
    );
    expect(screen.getByRole("tab", { name: /citations 2/i })).toBeInTheDocument();
    expect(screen.getByText("citations-body")).toBeInTheDocument();
  });
});
