import { Button, Modal, Select, Text, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { useDeleteWorkspace, useSettings, useUpdateSettings } from "../api/settings";

const STATUSES = ["drafting", "submitted", "awarded", "rejected", "abandoned"];
const CITATION_STYLES = ["vancouver", "author-year"];

export function WorkspaceSettingsModal({
  id,
  opened,
  onClose,
}: {
  id: string;
  opened: boolean;
  onClose: () => void;
}) {
  const settings = useSettings(id);
  const update = useUpdateSettings(id);
  const del = useDeleteWorkspace(id);
  const navigate = useNavigate();
  const [title, setTitle] = useState("");
  const [status, setStatus] = useState("drafting");
  const [deadline, setDeadline] = useState("");
  const [citationStyle, setCitationStyle] = useState("vancouver");

  // Populate from the loaded settings (and re-sync if they change / modal reopens).
  useEffect(() => {
    const d = settings.data;
    if (!d) return;
    setTitle(d.title);
    setStatus(d.status);
    setDeadline(d.deadline ?? "");
    setCitationStyle(d.citation_style);
  }, [settings.data, opened]);

  return (
    <Modal opened={opened} onClose={onClose} title="Workspace settings">
      <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      <Select
        label="Status"
        data={STATUSES}
        value={status}
        onChange={(v) => setStatus(v ?? "drafting")}
        allowDeselect={false}
      />
      <TextInput
        label="Deadline"
        type="date"
        value={deadline}
        onChange={(e) => setDeadline(e.currentTarget.value)}
      />
      <Select
        label="Citation style"
        data={CITATION_STYLES}
        value={citationStyle}
        onChange={(v) => setCitationStyle(v ?? "vancouver")}
        allowDeselect={false}
      />
      {update.isError ? <Text c="red" size="sm">Could not save settings.</Text> : null}
      <Button
        disabled={update.isPending}
        onClick={() =>
          update.mutate(
            {
              title,
              status,
              deadline: deadline === "" ? null : deadline,
              citation_style: citationStyle,
            },
            { onSuccess: onClose },
          )
        }
      >
        Save
      </Button>
      <div style={{ marginTop: "1.5rem", borderTop: "1px solid var(--mantine-color-red-9)", paddingTop: "0.75rem" }}>
        <p style={{ fontSize: "0.85rem", opacity: 0.8 }}>
          Deletes the workspace: it disappears for everyone, collaborators lose access, and any
          running work stops. Its contents are kept on the server rather than erased. You cannot
          undo this.
        </p>
        {del.isError ? <Text c="red" size="sm">Could not delete workspace.</Text> : null}
        <Button
          color="red"
          variant="outline"
          disabled={del.isPending}
          onClick={() => {
            const name = settings.data?.title ?? "";
            if (
              window.confirm(
                `Delete "${name}"? Everyone loses access immediately and you cannot undo this. ` +
                  `The contents are kept on the server rather than erased.`,
              )
            ) {
              del.mutate(undefined, {
                onSuccess: () => {
                  onClose();
                  navigate("/cockpit");
                },
              });
            }
          }}
        >
          Delete workspace
        </Button>
      </div>
    </Modal>
  );
}
