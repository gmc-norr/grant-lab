import { Button, Modal, Text, TextInput, Textarea } from "@mantine/core";
import { useState } from "react";
import { useApproveMetadata } from "../api/references";

export function MetadataModal({
  id,
  referenceId,
  opened,
  onClose,
}: {
  id: string;
  referenceId: string;
  opened: boolean;
  onClose: () => void;
}) {
  const approve = useApproveMetadata(id);
  const [title, setTitle] = useState("");
  const [authors, setAuthors] = useState("");
  const [year, setYear] = useState("");
  const [venue, setVenue] = useState("");
  const [doi, setDoi] = useState("");

  return (
    <Modal opened={opened} onClose={onClose} title="Edit reference metadata">
      <TextInput label="Title" value={title} onChange={(e) => setTitle(e.currentTarget.value)} />
      <Textarea
        label="Authors (one per line: Family, Given)"
        value={authors}
        onChange={(e) => setAuthors(e.currentTarget.value)}
      />
      <TextInput label="Year" value={year} onChange={(e) => setYear(e.currentTarget.value)} />
      <TextInput label="Venue" value={venue} onChange={(e) => setVenue(e.currentTarget.value)} />
      <TextInput label="DOI" value={doi} onChange={(e) => setDoi(e.currentTarget.value)} />
      {approve.isError ? <Text c="red" size="sm">Could not save — check the title.</Text> : null}
      <Button
        disabled={approve.isPending}
        onClick={() =>
          approve.mutate(
            {
              referenceId,
              title,
              authors: authors.split("\n").map((s) => s.trim()).filter(Boolean),
              year,
              venue,
              doi,
            },
            { onSuccess: onClose },
          )
        }
      >
        Save metadata
      </Button>
    </Modal>
  );
}
