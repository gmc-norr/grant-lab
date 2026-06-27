import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { Button, Modal } from "@mantine/core";

import { useReferences } from "../api/citations";
import { useDraft, useSpec } from "../api/editor";
import { type ScorecardCriterion, useScorecard } from "../api/evaluator";
import { type RubricCriterion, useRubric, useUpdateRubric } from "../api/rubric";
import { useAddMember, useRemoveMember } from "../api/settings";
import { useCockpitMeta, useMembers, useVersions } from "../api/workspace";
import { useCockpitNav } from "./cockpitNav";
import styles from "./CockpitSidebar.module.css";
import { useEditorDirty } from "./editorStore";
import { RubricEditor } from "./RubricEditor";
import { SidebarGroup } from "./SidebarGroup";

const BAND_CLASS: Record<ScorecardCriterion["band"], string> = {
  strong: "band-strong",
  adequate: "band-adequate",
  at_risk: "band-at-risk",
  gap: "band-gap",
};
const BAND_LABEL: Record<ScorecardCriterion["band"], string> = {
  strong: "Strong",
  adequate: "Adequate",
  at_risk: "At-risk",
  gap: "Gap",
};

// The cockpit's left sidebar (the route's handle.Sidebar). Reads the SAME queries +
// ?tab/?sec as the editor/panels — React Query dedupes, the URL is the shared channel —
// so everything stays in sync across the AppShell boundary with no props. All hooks are
// always called (stable order); on scorecard/review tabs the Outline shows Draft sections.
export function CockpitSidebar() {
  const { id = "" } = useParams();
  const { tab, sec, navigate } = useCockpitNav();
  const artifact: "draft" | "spec" = tab === "spec" ? "spec" : "draft";
  const draft = useDraft(id);
  const spec = useSpec(id);
  const q = artifact === "spec" ? spec : draft;
  // Subscribe to ONLY the dirty set (not cursor) so a caret move does not re-render
  // the whole sidebar (the cursor updates on every keystroke).
  const dirty = useEditorDirty();
  const sections = q.data?.sections ?? [];

  const references = useReferences(id);
  // Array.isArray guards keep the never-500 sidebar robust to an unexpected response
  // shape (and keep the Phase-3a tests — which stub every GET with the draft fixture —
  // green); criteria uses deep optional chaining for the same reason.
  const refs = Array.isArray(references.data) ? references.data : [];
  const scorecard = useScorecard(id);
  const criteria = scorecard.data?.scorecard?.criteria ?? [];

  const versions = useVersions(id);
  const versionRows = Array.isArray(versions.data) ? versions.data : [];
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

  const isOwnerActive = meta.data?.role === "owner" && meta.data?.current_phase === "active";
  const [editOpen, setEditOpen] = useState(false);
  const rubric = useRubric(id);
  const updateRubric = useUpdateRubric(id);
  const [draftCriteria, setDraftCriteria] = useState<RubricCriterion[]>([]);
  const [draftNotes, setDraftNotes] = useState("");
  useEffect(() => {
    if (editOpen && rubric.data) {
      setDraftCriteria(rubric.data.criteria);
      setDraftNotes(rubric.data.funder_notes ?? "");
    }
  }, [editOpen, rubric.data]);

  return (
    <div className={styles.sidebar}>
      <SidebarGroup title="Outline" count={sections.length || undefined}>
        {q.isLoading && <p className={styles.note}>Loading…</p>}
        {!q.isLoading && sections.length === 0 && <p className={styles.note}>No sections.</p>}
        <ul className={styles.outline} aria-label="Outline">
          {sections.map((s) => (
            <li key={s.index}>
              <button
                type="button"
                className={styles.row}
                aria-current={sec === s.index}
                onClick={() => navigate({ top_tab: artifact, section_index: s.index })}
              >
                <span className={styles.rowLabel}>{s.heading || "preamble"}</span>
                {dirty.includes(s.index) && (
                  <span className={styles.dirty} aria-label="unsaved">
                    •
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="References" count={refs.length || undefined} defaultOpen={false}>
        {refs.length === 0 && <p className={styles.note}>No references.</p>}
        <ul className={styles.outline} aria-label="References">
          {refs.map((r) => (
            <li key={r.id} className={styles.metaRow}>
              {r.citekey && <span className={styles.metaSub}>{r.citekey}</span>}
              <span className={styles.rowLabel}>{r.title}</span>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Criteria" count={criteria.length || undefined} defaultOpen={false}>
        {isOwnerActive && (
          <button type="button" className={styles.editRubric} onClick={() => setEditOpen(true)}>
            Edit rubric
          </button>
        )}
        {criteria.length === 0 && <p className={styles.note}>No criteria yet.</p>}
        <ul className={styles.outline} aria-label="Criteria">
          {criteria.map((c) => (
            <li key={c.index}>
              <button
                type="button"
                className={styles.criterion}
                onClick={() => navigate({ top_tab: "scorecard" })}
              >
                <span className={`band-pill ${BAND_CLASS[c.band]}`}>{BAND_LABEL[c.band]}</span>
                <span className={styles.criterionName}>{c.name}</span>
              </button>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Versions" count={versionRows.length || undefined} defaultOpen={false}>
        {versionRows.length === 0 && <p className={styles.note}>No versions yet.</p>}
        <ul className={styles.outline} aria-label="Versions">
          {versionRows.map((v) => (
            <li key={v.commit_sha} className={styles.metaRow}>
              <span className={styles.rowLabel}>{v.message}</span>
              <span className={styles.metaSub}>{v.created_by}</span>
            </li>
          ))}
        </ul>
      </SidebarGroup>

      <SidebarGroup title="Members" count={memberCount} defaultOpen={false}>
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
      </SidebarGroup>

      {isOwnerActive && editOpen && (
        <Modal opened onClose={() => setEditOpen(false)} title="Edit funder criteria" size="xl">
          <RubricEditor
            criteria={draftCriteria}
            funderNotes={draftNotes}
            onChange={(n) => { setDraftCriteria(n.criteria); setDraftNotes(n.funderNotes); }}
          />
          {updateRubric.isError && <p className={styles.note}>Could not save the rubric.</p>}
          <Button
            disabled={updateRubric.isPending}
            onClick={() => updateRubric.mutate(
              { criteria: draftCriteria, funder_notes: draftNotes || null },
              { onSuccess: () => setEditOpen(false) },
            )}
          >
            Save
          </Button>
        </Modal>
      )}
    </div>
  );
}
