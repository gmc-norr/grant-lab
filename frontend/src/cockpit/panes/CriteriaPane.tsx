import { useEffect, useState } from "react";

import { Button, Modal } from "@mantine/core";

import { useScorecard } from "../../api/evaluator";
import { type RubricCriterion, useRubric, useUpdateRubric } from "../../api/rubric";
import { useCockpitMeta } from "../../api/workspace";
import { BandBadge } from "../BandBadge";
import { useCockpitNav } from "../cockpitNav";
import styles from "../CockpitSidebar.module.css";
import { RubricEditor } from "../RubricEditor";
import { PaneHeader } from "./PaneHeader";

export function CriteriaPane({ id }: { id: string }) {
  const { navigate } = useCockpitNav();
  const scorecard = useScorecard(id);
  const criteria = scorecard.data?.scorecard?.criteria ?? [];

  const meta = useCockpitMeta(id);
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
    <>
      <PaneHeader title="Criteria" count={criteria.length || undefined} />
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
              <BandBadge band={c.band} />
              <span className={styles.criterionName}>{c.name}</span>
            </button>
          </li>
        ))}
      </ul>

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
    </>
  );
}
