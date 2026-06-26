import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useCreateProfile, type ProfileCreateBody } from "../api/profiles";
import styles from "./ProfileForm.module.css";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];

const EMPTY_STRUCT: StructuredValues = {
  areas: "", career_stage: "", applicant_types: "", role: "", years_since_phd: "",
  org_type: "", capabilities: "", patient_populations: "", scale: "",
  funding_instruments: "", funder_exclusions: "", country: "", host_institution: "",
};

export function ProfileNew() {
  const navigate = useNavigate();
  const create = useCreateProfile();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("organizational_unit");
  const [level, setLevel] = useState("");
  const [missionText, setMissionText] = useState("");
  const [orcids, setOrcids] = useState("");
  const [namesAffiliations, setNamesAffiliations] = useState("");
  const [pastedGrants, setPastedGrants] = useState("");
  const [struct, setStruct] = useState<StructuredValues>(EMPTY_STRUCT);
  const setS = (k: keyof StructuredValues, v: string) => setStruct((p) => ({ ...p, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileCreateBody = {
      name, kind, level, orcids, names_affiliations: namesAffiliations,
      pasted_grants: pastedGrants, mission_text: missionText, ...struct,
    };
    create.mutate(body, { onSuccess: (out) => navigate(`/profiles/${out.id}/edit`) });
  }

  return (
    <main className={styles.page}>
      <Link className={styles.back} to="/profiles">← All profiles</Link>
      <h1>Create a profile</h1>
      <p className={styles.lede}>
        Pick a kind, add seed data, and the local LLM drafts a description plus rubric. You can edit before it
        goes live.
      </p>
      <form className={styles.form} onSubmit={submit}>
        <label>Profile name
          <input type="text" required maxLength={200} value={name}
                 placeholder="e.g. Digital Pathology Lab" onChange={(e) => setName(e.target.value)} />
        </label>
        <label>Kind
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="organizational_unit">Organizational unit</option>
            <option value="individual">Individual researcher</option>
          </select>
        </label>
        {kind === "organizational_unit" && (
          <>
            <label>Level
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
            <label>Mission / strategy / capabilities <small>(free text)</small>
              <textarea rows={4} value={missionText} onChange={(e) => setMissionText(e.target.value)} />
            </label>
          </>
        )}
        <StructuredFields kind={kind} values={struct} onChange={setS} />
        <label>Member ORCIDs <small>(one per line; for an individual, your own)</small>
          <textarea rows={2} value={orcids} placeholder="0000-0001-2345-6789" onChange={(e) => setOrcids(e.target.value)} />
        </label>
        <label>Members by name <small>(one per line: Last F | Affiliation)</small>
          <textarea rows={2} value={namesAffiliations} onChange={(e) => setNamesAffiliations(e.target.value)} />
        </label>
        <label>Pasted past grants <small>(optional, titles + abstracts)</small>
          <textarea rows={4} value={pastedGrants} onChange={(e) => setPastedGrants(e.target.value)} />
        </label>
        <div className={styles.actions}>
          <button type="submit" disabled={create.isPending}>Bootstrap profile</button>
          {create.isError && (
            <span className={styles.err} role="alert">
              {(create.error as Error | null)?.message ?? "Couldn't create the profile."}
            </span>
          )}
        </div>
      </form>
    </main>
  );
}
