import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { useActivate, useRubric, useSetupStatus, type RubricCriterion } from "../api/rubric";
import { useCockpitMeta } from "../api/workspace";
import { RubricEditor } from "./RubricEditor";

export function WorkspaceSetup({ id }: { id: string }) {
  const qc = useQueryClient();
  const meta = useCockpitMeta(id);
  const isOwner = meta.data?.role === "owner";
  const setup = useSetupStatus(id);
  const status = setup.data?.status;

  // Viewer (and owner) auto-advance: when setup completes, flip the phase branch.
  useEffect(() => {
    if (status === "done") qc.invalidateQueries({ queryKey: ["cockpit-meta", id] });
  }, [status, id, qc]);

  if (status === "failed")
    return (
      <main className="ws-setup">
        <h1>Setup failed</h1>
        {isOwner && setup.data?.last_error && <p className="ws-setup-error">{setup.data.last_error}</p>}
        <p>Delete this workspace and create it again, or contact an administrator.</p>
      </main>
    );

  if (status === "done")
    return <main className="ws-setup"><p aria-busy="true">Opening cockpit…</p></main>;

  if (status === "waiting_for_rubric_review" && isOwner)
    return <OwnerRubricGate id={id} />;

  if (status === "waiting_for_rubric_review")
    return <main className="ws-setup"><p aria-busy="true">Waiting for the owner to review the rubric…</p></main>;

  return <main className="ws-setup"><p aria-busy="true">Setting up your workspace… (usually under a minute)</p></main>;
}

function OwnerRubricGate({ id }: { id: string }) {
  const rubric = useRubric(id);
  const activate = useActivate(id);
  const [criteria, setCriteria] = useState<RubricCriterion[]>([]);
  const [funderNotes, setFunderNotes] = useState("");

  useEffect(() => {
    if (rubric.data) {
      setCriteria(rubric.data.criteria);
      setFunderNotes(rubric.data.funder_notes ?? "");
    }
  }, [rubric.data]);

  if (rubric.isLoading) return <main className="ws-setup"><p>Loading the rubric…</p></main>;

  return (
    <main className="ws-setup">
      <h1>Review the funder criteria</h1>
      <p className="ws-setup-help">Weights are relative — the % shows each criterion's share.</p>
      <RubricEditor
        criteria={criteria}
        funderNotes={funderNotes}
        onChange={(n) => { setCriteria(n.criteria); setFunderNotes(n.funderNotes); }}
      />
      {activate.isError && <p className="ws-setup-error">Could not activate. Check the criteria and retry.</p>}
      <button type="button" disabled={activate.isPending}
        onClick={() => activate.mutate({ criteria, funder_notes: funderNotes || null })}>
        Confirm &amp; activate
      </button>
    </main>
  );
}
