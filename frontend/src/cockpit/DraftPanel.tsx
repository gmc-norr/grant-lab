import { Box, Button, Group, Menu, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";

import { useDraft, useSaveSection } from "../api/editor";
import { downloadExport, type ExportKind } from "../api/export";
import { CollabEditor } from "../collab/CollabEditor";
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

  if (draft.isLoading)
    return (
      <Box component="main" p="md">
        <Text aria-busy="true">Loading cockpit…</Text>
      </Box>
    );
  if (draft.isError || !draft.data) {
    return (
      <Box component="main" p="md">
        <Title order={2} role="alert">
          Could not load this workspace
        </Title>
      </Box>
    );
  }
  const { head, can_edit, sections } = draft.data;
  const exportActions = (
    <>
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
        <Text role="status" size="sm" c="dimmed">
          {exportMsg}
        </Text>
      )}
    </>
  );
  return (
    <Stack gap="sm">
      {draft.data.collab_enabled ? (
        <CollabEditor
          key={`collab-draft:${id}`}
          workspaceId={id}
          artifact="draft"
          canWrite={draft.data.can_write}
          head={head}
          sections={sections}
          canEdit={can_edit}
          actions={exportActions}
        />
      ) : (
        <>
          <Group gap="sm">{exportActions}</Group>
          <ArtifactSectionEditor
            key={`draft:${id}`}
            workspaceId={id}
            branch={draft.data.branch}
            queryKey={["draft", id]}
            head={head}
            canEdit={can_edit}
            sections={sections}
            save={save}
            refetch={draft.refetch}
            citations
            openPeek={openPeek}
          />
        </>
      )}
    </Stack>
  );
}
