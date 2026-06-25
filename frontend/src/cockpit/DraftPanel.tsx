import { Button, Menu } from "@mantine/core";
import { useState } from "react";

import { useDraft, useSaveSection } from "../api/editor";
import { downloadExport, type ExportKind } from "../api/export";
import { ArtifactSectionEditor } from "../editor/ArtifactSectionEditor";

export function DraftPanel({ id, openPeek }: { id: string; openPeek?: { ev: string; n: number } | null }) {
  const draft = useDraft(id);
  const save = useSaveSection(id);
  const [exportMsg, setExportMsg] = useState<string | null>(null);

  async function onExport(kind: ExportKind) {
    setExportMsg(null);
    const r = await downloadExport(id, kind);
    if (r.ok) return;
    if (r.kind === "blocked")
      setExportMsg(
        `Export blocked — approve these references first: ${r.blocked.map((b) => b.citekey).join(", ")}`,
      );
    else if (r.kind === "ledger_invalid") setExportMsg("citations.json is corrupt — re-save citations.");
    else if (r.kind === "no_draft") setExportMsg("No draft to export yet.");
    else setExportMsg("Export failed.");
  }

  if (draft.isLoading) return <main aria-busy="true">Loading cockpit…</main>;
  if (draft.isError || !draft.data) {
    return (
      <main role="alert">
        <h1>Could not load this workspace</h1>
      </main>
    );
  }
  const { head, can_edit, sections } = draft.data;
  return (
    <div className="draft-shell">
      <div className="draft-toolbar">
        <Menu position="bottom-start">
          <Menu.Target>
            <Button variant="default" size="xs">
              Export ▾
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item onClick={() => void onExport("raw")}>Markdown</Menu.Item>
            <Menu.Item onClick={() => void onExport("composed")}>
              Markdown + citations & bibliography
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
        {exportMsg && (
          <span role="status" className="export-msg">
            {exportMsg}
          </span>
        )}
      </div>
      <ArtifactSectionEditor
        key={`draft:${id}`}
        workspaceId={id}
        queryKey={["draft", id]}
        head={head}
        canEdit={can_edit}
        sections={sections}
        save={save}
        refetch={draft.refetch}
        citations
        openPeek={openPeek}
      />
    </div>
  );
}
