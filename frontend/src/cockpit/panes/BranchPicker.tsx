import { useState } from "react";

import { useBranches, useCreateBranch, useDeleteBranch, useSwitchBranch } from "../../api/branches";

type Confirm =
  | { kind: "switch"; name: string }
  | { kind: "delete"; name: string }
  | null;

/** A branch selector with confirm dialogs. Confirm dialogs are load-bearing:
 *  a switch disconnects every open editor (spec §5) and a delete drops commits
 *  (spec §3.2) — neither must fire on a single stray click. */
export function BranchPicker({ id }: { id: string }) {
  const { data } = useBranches(id);
  const createBranch = useCreateBranch(id);
  const switchBranch = useSwitchBranch(id);
  const deleteBranch = useDeleteBranch(id);

  const [menuOpen, setMenuOpen] = useState(false);
  const [confirm, setConfirm] = useState<Confirm>(null);
  const [newBranchOpen, setNewBranchOpen] = useState(false);
  const [newBranchName, setNewBranchName] = useState("");

  const current = data?.current ?? "…";
  const branches = data?.branches ?? [];
  const otherBranches = branches.filter((b) => b !== current);

  function handleSwitch() {
    if (confirm?.kind !== "switch") return;
    switchBranch.mutate(confirm.name);
    setConfirm(null);
  }

  function handleDelete() {
    if (confirm?.kind !== "delete") return;
    deleteBranch.mutate(confirm.name);
    setConfirm(null);
  }

  function handleCreate() {
    const name = newBranchName.trim();
    if (!name) return;
    createBranch.mutate(name, {
      onSuccess: () => {
        setNewBranchName("");
        setNewBranchOpen(false);
      },
    });
  }

  return (
    <div style={{ position: "relative" }}>
      {/* Trigger — accessible name contains the current branch */}
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((v) => !v)}
      >
        {current}
      </button>

      {/* Always-visible branch list so every branch name is findable */}
      <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
        {otherBranches.map((name) => (
          <li key={name}>
            <span>{name}</span>
          </li>
        ))}
      </ul>

      {/* Dropdown menu — visible when triggered */}
      {menuOpen && (
        <ul role="menu" style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {/* Switch items — one per non-current branch, labelled by branch name only */}
          {otherBranches.map((name) => (
            <li key={name}>
              <button
                type="button"
                role="menuitem"
                aria-label={name}
                onClick={() => {
                  setMenuOpen(false);
                  setConfirm({ kind: "switch", name });
                }}
              >
                {name}
              </button>
            </li>
          ))}
          {/* Delete items — one per non-current branch */}
          {otherBranches.map((name) => (
            <li key={`delete-${name}`}>
              <button
                type="button"
                role="menuitem"
                aria-label={`Delete ${name}`}
                onClick={() => {
                  setMenuOpen(false);
                  setConfirm({ kind: "delete", name });
                }}
              >
                Delete {name}
              </button>
            </li>
          ))}
          <li>
            <button
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setNewBranchOpen(true);
              }}
            >
              New branch…
            </button>
          </li>
        </ul>
      )}

      {/* Switch confirmation dialog */}
      {confirm?.kind === "switch" && (
        <div role="dialog" aria-modal="true" aria-label="Switch branch">
          <p>
            Switching to <strong>{confirm.name}</strong> will disconnect every open editor.
            Unsaved keystrokes will be lost.
          </p>
          <button type="button" onClick={handleSwitch} disabled={switchBranch.isPending}>
            Switch branch
          </button>
          <button type="button" onClick={() => setConfirm(null)}>
            Cancel
          </button>
        </div>
      )}

      {/* Delete confirmation dialog */}
      {confirm?.kind === "delete" && (
        <div role="dialog" aria-modal="true" aria-label="Delete branch">
          <p>
            Delete <strong>{confirm.name}</strong>? This cannot be undone — the commits on this
            branch will be permanently lost.
          </p>
          <button type="button" onClick={handleDelete} disabled={deleteBranch.isPending}>
            Delete branch
          </button>
          <button type="button" onClick={() => setConfirm(null)}>
            Cancel
          </button>
        </div>
      )}

      {/* New branch dialog */}
      {newBranchOpen && (
        <div role="dialog" aria-modal="true" aria-label="New branch">
          <label htmlFor="branch-name-input">Branch name</label>
          <input
            id="branch-name-input"
            type="text"
            value={newBranchName}
            onChange={(e) => setNewBranchName(e.currentTarget.value)}
            placeholder="my-branch"
          />
          <button
            type="button"
            onClick={handleCreate}
            disabled={createBranch.isPending || !newBranchName.trim()}
          >
            Create
          </button>
          <button type="button" onClick={() => setNewBranchOpen(false)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
