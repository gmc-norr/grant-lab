import { useState } from "react";

import { useAddMember, useRemoveMember } from "../../api/settings";
import { useCockpitMeta, useMembers } from "../../api/workspace";
import styles from "../CockpitSidebar.module.css";
import { PaneHeader } from "./PaneHeader";

export function MembersPane({ id }: { id: string }) {
  const members = useMembers(id);
  const meta = useCockpitMeta(id);
  // Owners get the named list; non-owner members 403 on /members and fall back to the
  // non-PII member_count from cockpit-meta. Array.isArray guard keeps it robust (and the
  // 3a draft-fixture mock → memberRows=null → the count branch, never a .map crash).
  const memberRows = Array.isArray(members.data) ? members.data : null;
  const memberCount = meta.data?.member_count;
  const addMember = useAddMember(id);
  const removeMember = useRemoveMember(id);
  const [newEmail, setNewEmail] = useState("");

  return (
    <>
      <PaneHeader title="Members" count={memberCount} />
      {memberRows ? (
        <>
          <ul className={styles.outline} aria-label="Members">
            {memberRows.map((mbr) => (
              <li key={mbr.email} className={styles.metaRow}>
                <span className={styles.rowLabel}>{mbr.email}</span>
                <span className={styles.metaSub}>{mbr.role}</span>
                {mbr.role === "member" && (
                  <button
                    type="button"
                    className={styles.memberRemove}
                    aria-label={`Remove ${mbr.email}`}
                    disabled={removeMember.isPending}
                    onClick={() => {
                      if (window.confirm(`Remove ${mbr.email}?`)) removeMember.mutate(mbr.user_id);
                    }}
                  >
                    ×
                  </button>
                )}
              </li>
            ))}
          </ul>
          <div className={styles.memberAdd}>
            <input
              aria-label="Add collaborator by email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="email…"
            />
            <button
              type="button"
              disabled={addMember.isPending || !newEmail.trim()}
              onClick={() =>
                addMember.mutate(newEmail.trim(), { onSuccess: () => setNewEmail("") })
              }
            >
              Add
            </button>
          </div>
          {addMember.isError && <p className={styles.note}>Could not add collaborator.</p>}
        </>
      ) : (
        <p className={styles.note}>
          {memberCount != null ? `${memberCount} member${memberCount === 1 ? "" : "s"}` : "—"}
        </p>
      )}
    </>
  );
}
