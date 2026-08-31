import { Button, Group, Menu, Modal, Text, TextInput } from "@mantine/core";
import { useEffect, useState } from "react";

import { useBranches, useCreateBranch, useDeleteBranch, useSwitchBranch } from "../api/branches";

type Confirm = { kind: "switch" | "delete"; name: string } | null;

/** The Versions pane's branch line: which branch you are on, and everything you
 *  can do to branches.
 *
 *  Replaces panes/BranchPicker, which listed every branch TWICE in one flat menu
 *  — once to switch to it, once to delete it, with nothing to tell them apart.
 *  The two lists are now separately labelled and the destructive one is last.
 *
 *  The confirm dialogs are load-bearing, not politeness: a switch disconnects
 *  every open editor, and a delete drops commits. Neither may fire on a stray
 *  click.
 */
export function BranchBar({
  id,
  branchFrom,
  onBranchFromHandled,
  switchTo,
  onSwitchHandled,
}: {
  id: string;
  /** Set by VersionGraph when a row's "Branch from here…" was chosen. */
  branchFrom: { sha: string; subject: string } | null;
  onBranchFromHandled: () => void;
  /** Set by VersionGraph when a branch pill was clicked. */
  switchTo: string | null;
  onSwitchHandled: () => void;
}) {
  const { data, isError } = useBranches(id);
  const createBranch = useCreateBranch(id);
  const switchBranch = useSwitchBranch(id);
  const deleteBranch = useDeleteBranch(id);

  const [confirm, setConfirm] = useState<Confirm>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState("");

  // A row asking to branch from a commit opens the same dialog the menu opens.
  useEffect(() => {
    if (branchFrom) setNewOpen(true);
  }, [branchFrom]);

  // A pill click asks for a switch; the dialog and its state stay here, so there
  // is ONE copy of the warning rather than two.
  useEffect(() => {
    if (switchTo) setConfirm({ kind: "switch", name: switchTo });
  }, [switchTo]);

  const current = data?.current ?? "…";
  const others = (data?.branches ?? []).filter((b) => b !== current);

  function closeNew() {
    setNewOpen(false);
    setNewName("");
    onBranchFromHandled();
  }

  /** Clearing `switchTo` is not tidiness. It is a plain string, so leaving it
   *  set makes a SECOND click on the same pill assign the same value: React
   *  sees no change, the effect does not re-run, and the dialog never opens.
   *  `branchFrom` escapes this only because it is a fresh object literal on
   *  every click — an accident, not a pattern to copy. */
  function closeSwitch() {
    setConfirm(null);
    onSwitchHandled();
  }

  function handleCreate() {
    const name = newName.trim();
    if (!name) return;
    createBranch.mutate({ name, fromSha: branchFrom?.sha }, { onSuccess: closeNew });
  }

  return (
    <>
      <Group px="sm" pb={6} gap={6} wrap="nowrap">
        <Menu position="bottom-start" width={240} withinPortal>
          <Menu.Target>
            <Button variant="subtle" size="xs" disabled={isError} aria-label={`Branch: ${current}`}>
              ⑂ {current} ▾
            </Button>
          </Menu.Target>
          <Menu.Dropdown>
            {others.length > 0 && <Menu.Label>Switch to</Menu.Label>}
            {others.map((name) => (
              <Menu.Item
                key={name}
                aria-label={name}
                onClick={() => setConfirm({ kind: "switch", name })}
              >
                ⑂ {name}
              </Menu.Item>
            ))}
            <Menu.Divider />
            <Menu.Item onClick={() => setNewOpen(true)}>Create new branch…</Menu.Item>
            {/* Destructive group last, and labelled. BranchPicker listed every
                branch twice in one flat run of items with nothing to tell a
                switch from a delete; the labels are the whole fix. */}
            {others.length > 0 && <Menu.Divider />}
            {others.length > 0 && <Menu.Label c="var(--app-bad)">Delete</Menu.Label>}
            {others.map((name) => (
              <Menu.Item
                key={`del-${name}`}
                color="red"
                aria-label={`Delete ${name}`}
                onClick={() => setConfirm({ kind: "delete", name })}
              >
                Delete {name}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>
      </Group>

      {/* `opened` keys off `confirm`, never off `switchTo`: the two update in
          different renders, so keying off the request would flash the dialog
          open with a null name. */}
      <Modal opened={confirm?.kind === "switch"} onClose={closeSwitch} title="Switch branch">
        <Text size="sm">
          Switching to <strong>{confirm?.name}</strong> will disconnect every open editor. Unsaved
          keystrokes will be lost.
        </Text>
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={closeSwitch}>
            Cancel
          </Button>
          <Button
            disabled={switchBranch.isPending}
            onClick={() => {
              if (confirm) switchBranch.mutate(confirm.name);
              closeSwitch();
            }}
          >
            Switch branch
          </Button>
        </Group>
      </Modal>

      <Modal opened={confirm?.kind === "delete"} onClose={() => setConfirm(null)} title="Delete branch">
        <Text size="sm">
          Delete <strong>{confirm?.name}</strong>? This cannot be undone — the commits on this branch
          will be permanently lost.
        </Text>
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={() => setConfirm(null)}>
            Cancel
          </Button>
          <Button
            color="red"
            disabled={deleteBranch.isPending}
            onClick={() => {
              if (confirm) deleteBranch.mutate(confirm.name);
              setConfirm(null);
            }}
          >
            Delete branch
          </Button>
        </Group>
      </Modal>

      <Modal opened={newOpen} onClose={closeNew} title="New branch">
        {branchFrom && (
          <Text size="xs" c="dimmed" mb="xs">
            Branching from {branchFrom.sha.slice(0, 7)} · {branchFrom.subject}
          </Text>
        )}
        <TextInput
          label="Branch name"
          placeholder="my-branch"
          value={newName}
          onChange={(e) => setNewName(e.currentTarget.value)}
        />
        <Group mt="md" justify="flex-end">
          <Button variant="default" onClick={closeNew}>
            Cancel
          </Button>
          <Button disabled={createBranch.isPending || !newName.trim()} onClick={handleCreate}>
            Create
          </Button>
        </Group>
      </Modal>
    </>
  );
}
