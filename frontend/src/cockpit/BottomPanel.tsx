import { Box, Button, Group, ScrollArea, Tabs } from "@mantine/core";

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
    <Box
      component="section"
      aria-label="Workspace panel"
      style={{
        // Fill the resizable bottom Panel: a fixed tab bar over a flex content
        // area that scrolls internally, so the panel's height follows the drag
        // handle instead of a fixed cap.
        height: "100%",
        display: "flex",
        flexDirection: "column",
        borderTop: "1px solid var(--app-border)",
        background: "var(--app-editor)",
      }}
    >
      <Group
        justify="space-between"
        gap="sm"
        px="sm"
        py={4}
        style={{ background: "var(--app-tabbar)", flex: "0 0 auto" }}
      >
        <Tabs value={activeTab} onChange={(v) => v && onTab(v as BottomTab)}>
          <Tabs.List aria-label="Panel">
            <Tabs.Tab value="inbox">Inbox {inboxCount}</Tabs.Tab>
            <Tabs.Tab value="problems">Problems {problemsCount}</Tabs.Tab>
            <Tabs.Tab value="activity">Activity</Tabs.Tab>
            <Tabs.Tab value="library">Library</Tabs.Tab>
            <Tabs.Tab value="citations">Citations {auditCount}</Tabs.Tab>
            <Tabs.Tab value="claims">Claims {claimsCount}</Tabs.Tab>
          </Tabs.List>
        </Tabs>
        <Button
          variant="subtle"
          size="xs"
          aria-expanded={!collapsed}
          onClick={onToggleCollapsed}
        >
          {collapsed ? "Expand panel" : "Collapse panel"}
        </Button>
      </Group>
      {!collapsed && (
        <ScrollArea p="sm" style={{ flex: 1, minHeight: 0 }}>
          {activeTab === "inbox" && <InboxPanel id={id} count={inboxCount} />}
          {activeTab === "problems" && <ProblemsPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "activity" && <ActivityPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "library" && <LibraryPanel id={id} />}
          {activeTab === "citations" && <CitationsPanel id={id} onNavigate={onNavigate} />}
          {activeTab === "claims" && <ClaimsPanel id={id} />}
        </ScrollArea>
      )}
    </Box>
  );
}
