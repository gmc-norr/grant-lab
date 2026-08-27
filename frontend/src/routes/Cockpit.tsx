import { useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import type { ImperativePanelHandle } from "react-resizable-panels";

import { Tabs } from "@mantine/core";

import { useCockpitMeta } from "../api/workspace";
import { BottomPanel } from "../cockpit/BottomPanel";
import { useCockpitNav } from "../cockpit/cockpitNav";
import { DraftPanel } from "../cockpit/DraftPanel";
import { confirmDiscardIfDirty } from "../cockpit/editorStore";
import type { BottomTab, NavTarget, TopTab } from "../cockpit/nav";
import { ReviewPanel } from "../cockpit/ReviewPanel";
import { ScorecardPanel } from "../cockpit/ScorecardPanel";
import { SpecPanel } from "../cockpit/SpecPanel";
import { UnsavedChangesGuard } from "../cockpit/UnsavedChangesGuard";
import { WorkspaceSetup } from "../cockpit/WorkspaceSetup";
import styles from "./Cockpit.module.css";

export function Cockpit() {
  const { id = "" } = useParams();
  const meta = useCockpitMeta(id);
  // Artifact tab + active section live in the URL (?tab/?sec) so the shell sidebar
  // Outline and the editor stay in sync across the AppShell boundary. The bottom
  // region (tab + collapse) stays local — it never crosses that boundary.
  const { tab, setTab, navigate } = useCockpitNav();
  const [bottomTab, setBottomTab] = useState<BottomTab>("inbox");
  const [collapsed, setCollapsed] = useState(false);
  const bottomRef = useRef<ImperativePanelHandle>(null);
  const peekNonce = useRef(0);
  const [openPeek, setOpenPeek] = useState<{ ev: string; n: number } | null>(null);

  // Switching the artifact tab unmounts the active editor and discards its unsaved
  // per-section buffers. That is NOT a router navigation (the router blocker can't
  // see it), so confirm here before discarding. A clean editor → no prompt.
  const onTab = (t: TopTab) => {
    if (t === tab) return;
    if (!confirmDiscardIfDirty()) return;
    setTab(t);
  };

  const onNavigate = (target: NavTarget) => {
    navigate(target); // ?tab + ?sec
    if (target.bottom_tab) {
      setBottomTab(target.bottom_tab);
      bottomRef.current?.expand(); // surface the panel if it was collapsed
    }
    if (target.evidence_id) {
      peekNonce.current += 1;
      setOpenPeek({ ev: target.evidence_id, n: peekNonce.current });
    }
  };

  // The bottom panel collapses to just its tab bar. Drive collapse through the
  // panel's imperative handle (like the shell sidebar/chat) so the drag handle,
  // the Collapse/Expand button, and the content visibility all agree; the panel's
  // onCollapse/onExpand callbacks are the single source of truth for `collapsed`.
  const toggleBottom = () => {
    const p = bottomRef.current;
    if (!p) return;
    if (p.isCollapsed()) p.expand();
    else p.collapse();
  };

  // Branch AFTER all hooks. The shell sidebar/right-pane are mounted by AppShell
  // from the route handle (router.tsx) and handle setup themselves (Task 10).
  if (meta.isLoading || !meta.data) return <p className="cockpit-loading">Loading…</p>;
  if (meta.data.current_phase === "setup") return <WorkspaceSetup id={id} />;

  return (
    <PanelGroup direction="vertical" autoSaveId="cm-cockpit-vertical" className={styles.cockpit}>
      <Panel id="editor" order={1} minSize={20} defaultSize={72} className={styles.editorPane}>
        <UnsavedChangesGuard />
        <Tabs
          value={tab}
          onChange={(v) => v && onTab(v as TopTab)}
          keepMounted={false}
        >
          <Tabs.List aria-label="Artifact">
            <Tabs.Tab value="draft">Draft</Tabs.Tab>
            <Tabs.Tab value="spec">Spec</Tabs.Tab>
            <Tabs.Tab value="scorecard">Scorecard</Tabs.Tab>
            <Tabs.Tab value="review">Panel review</Tabs.Tab>
          </Tabs.List>
        </Tabs>
        <div className={styles.editorScroll} data-testid="editor-scroll">
          {tab === "draft" && <DraftPanel id={id} openPeek={openPeek} />}
          {tab === "spec" && <SpecPanel id={id} />}
          {tab === "scorecard" && <ScorecardPanel id={id} onNavigate={onNavigate} />}
          {tab === "review" && <ReviewPanel id={id} />}
        </div>
      </Panel>
      <PanelResizeHandle className={styles.vHandle} aria-label="Resize panel" />
      <Panel
        id="bottom"
        order={2}
        collapsible
        collapsedSize={6}
        minSize={12}
        defaultSize={28}
        ref={bottomRef}
        className={styles.bottomPane}
        onCollapse={() => setCollapsed(true)}
        onExpand={() => setCollapsed(false)}
      >
        <BottomPanel
          id={id}
          activeTab={bottomTab}
          onTab={setBottomTab}
          collapsed={collapsed}
          onToggleCollapsed={toggleBottom}
          onNavigate={onNavigate}
        />
      </Panel>
    </PanelGroup>
  );
}
