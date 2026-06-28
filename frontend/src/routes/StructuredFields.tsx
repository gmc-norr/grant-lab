import { Fieldset, NativeSelect, Stack, Textarea, TextInput } from "@mantine/core";

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

const toData = (pairs: string[][]) => pairs.map(([value, label]) => ({ value, label }));

export function StructuredFields({
  kind, values, onChange, disabled,
}: {
  kind: string;
  values: StructuredValues;
  onChange: (k: keyof StructuredValues, v: string) => void;
  disabled?: boolean;
}) {
  const set = (k: keyof StructuredValues) => (e: { currentTarget: { value: string } }) => onChange(k, e.currentTarget.value);
  return (
    <>
      {kind === "organizational_unit" && (
        <Fieldset disabled={disabled} mb="sm">
          <Stack gap="sm">
            <NativeSelect label="Organization type" data={toData(ORG_TYPES)} value={values.org_type} onChange={set("org_type")} />
            <Textarea label="Capabilities" description="(one per line)" rows={3} value={values.capabilities} onChange={set("capabilities")} />
            <Textarea label="Patient populations" description="(one per line)" rows={2} value={values.patient_populations} onChange={set("patient_populations")} />
            <TextInput label="Scale" description="(free text)" maxLength={200} value={values.scale} onChange={set("scale")} />
          </Stack>
        </Fieldset>
      )}
      {kind === "individual" && (
        <Fieldset disabled={disabled} mb="sm">
          <Stack gap="sm">
            <NativeSelect label="Career stage" data={toData(CAREER_STAGES)} value={values.career_stage} onChange={set("career_stage")} />
            <TextInput label="Role" description="(free text)" maxLength={200} value={values.role} onChange={set("role")} />
            <TextInput label="Years since PhD" description="(0–80)" value={values.years_since_phd} onChange={set("years_since_phd")} />
          </Stack>
        </Fieldset>
      )}
      <Stack gap="sm">
        <TextInput label="Host institution" description="(free text)" maxLength={200} value={values.host_institution} onChange={set("host_institution")} />
        <TextInput label="Country (e.g. SE)" maxLength={200} value={values.country} onChange={set("country")} />
        <Textarea label="Applicant types" description="(one per line: solo_pi, consortium_coordinator, consortium_partner, institution)" rows={2} value={values.applicant_types} onChange={set("applicant_types")} />
        <Textarea label="Funding instruments" description="(one per line: project_grant, fellowship, mobility, infrastructure, programme)" rows={2} value={values.funding_instruments} onChange={set("funding_instruments")} />
        <Textarea label="Funder exclusions" description="(one per line)" rows={2} value={values.funder_exclusions} onChange={set("funder_exclusions")} />
        <Textarea label="Research areas" description="(one per line)" rows={3} value={values.areas} onChange={set("areas")} />
      </Stack>
    </>
  );
}
