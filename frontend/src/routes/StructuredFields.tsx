export type StructuredValues = {
  areas: string;
  career_stage: string;
  applicant_types: string;
  role: string;
  years_since_phd: string;
  org_type: string;
  capabilities: string;
  patient_populations: string;
  scale: string;
  funding_instruments: string;
  funder_exclusions: string;
  country: string;
  host_institution: string;
};

const ORG_TYPES = [
  ["", "(unspecified)"], ["research_organisation", "Research organisation"],
  ["university_hospital", "University hospital"], ["public_body", "Public body"],
  ["sme", "SME"], ["other", "Other"],
];
const CAREER_STAGES = [
  ["", "(unspecified)"], ["phd_student", "PhD student"], ["postdoc", "Postdoc"],
  ["junior_pi", "Junior PI"], ["senior_pi", "Senior PI"], ["group_leader", "Group leader"],
];

export function StructuredFields({
  kind, values, onChange, disabled,
}: {
  kind: string;
  values: StructuredValues;
  onChange: (k: keyof StructuredValues, v: string) => void;
  disabled?: boolean;
}) {
  const set = (k: keyof StructuredValues) => (e: { target: { value: string } }) => onChange(k, e.target.value);
  return (
    <>
      {kind === "organizational_unit" && (
        <fieldset disabled={disabled}>
          <label>Organization type
            <select value={values.org_type} onChange={set("org_type")}>
              {ORG_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label>Capabilities <small>(one per line)</small>
            <textarea rows={3} value={values.capabilities} onChange={set("capabilities")} />
          </label>
          <label>Patient populations <small>(one per line)</small>
            <textarea rows={2} value={values.patient_populations} onChange={set("patient_populations")} />
          </label>
          <label>Scale <small>(free text)</small>
            <input type="text" maxLength={200} value={values.scale} onChange={set("scale")} />
          </label>
        </fieldset>
      )}
      {kind === "individual" && (
        <fieldset disabled={disabled}>
          <label>Career stage
            <select value={values.career_stage} onChange={set("career_stage")}>
              {CAREER_STAGES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label>Role <small>(free text)</small>
            <input type="text" maxLength={200} value={values.role} onChange={set("role")} />
          </label>
          <label>Years since PhD <small>(0–80)</small>
            <input type="text" value={values.years_since_phd} onChange={set("years_since_phd")} />
          </label>
        </fieldset>
      )}
      <label>Host institution <small>(free text)</small>
        <input type="text" maxLength={200} value={values.host_institution} onChange={set("host_institution")} />
      </label>
      <label>Country (e.g. SE)
        <input type="text" maxLength={200} value={values.country} onChange={set("country")} />
      </label>
      <label>Applicant types <small>(one per line: solo_pi, consortium_coordinator, consortium_partner, institution)</small>
        <textarea rows={2} value={values.applicant_types} onChange={set("applicant_types")} />
      </label>
      <label>Funding instruments <small>(one per line: project_grant, fellowship, mobility, infrastructure, programme)</small>
        <textarea rows={2} value={values.funding_instruments} onChange={set("funding_instruments")} />
      </label>
      <label>Funder exclusions <small>(one per line)</small>
        <textarea rows={2} value={values.funder_exclusions} onChange={set("funder_exclusions")} />
      </label>
      <label>Research areas <small>(one per line)</small>
        <textarea rows={3} value={values.areas} onChange={set("areas")} />
      </label>
    </>
  );
}
