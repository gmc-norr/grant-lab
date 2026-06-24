import { Button, Modal, Select, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useSettings, useUpdateSettings } from "../api/settings";

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
      {update.isError ? <p className="library-error">Could not save settings.</p> : null}
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
    </Modal>
  );
}
