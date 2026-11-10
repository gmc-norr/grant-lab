import { Fieldset, MultiSelect, NativeSelect, Stack, Text, Textarea, TextInput } from "@mantine/core";

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

/** Which fields to render (UX review 2026-10-31). `eligibility` is the three
 *  facts the ranker's eligibility check reads -- organisation type, country and
 *  applicant types (see EligibilityWarning.tsx) -- shown up front, because left
 *  blank no call can be checked for eligibility; `rest` is everything optional, folded.
 *  `all` is both in one block (the default; only StructuredFields.test.tsx). */
export type StructuredPart = "all" | "eligibility" | "rest";

export function StructuredFields({
  kind, values, onChange, disabled, hideAreas = false, part = "all",
}: {
  kind: string;
  values: StructuredValues;
  onChange: (k: keyof StructuredValues, v: string) => void;
  disabled?: boolean;
  hideAreas?: boolean;
  part?: StructuredPart;
}) {
  const set = (k: keyof StructuredValues) => (e: { currentTarget: { value: string } }) => onChange(k, e.currentTarget.value);
  const orgType = <NativeSelect label="Organization type" data={toData(ORG_TYPES)} value={values.org_type} onChange={set("org_type")} />;
  const country = <TextInput label="Country (e.g. SE)" maxLength={200} value={values.country} onChange={set("country")} />;
  const applyingAs = (
    <MultiSelect label="Applying as" disabled={disabled} data={[
      { value: "solo_pi", label: "Individual investigator" }, { value: "consortium_coordinator", label: "Consortium lead" },
      { value: "consortium_partner", label: "Consortium partner" }, { value: "institution", label: "Institution" },
    ]} value={values.applicant_types.split("\n").filter(Boolean)} onChange={selected => onChange("applicant_types", selected.join("\n"))} />
  );
  if (part === "eligibility") {
    return (
      <Fieldset disabled={disabled} mb="sm" legend="Eligibility details" data-field="eligibility-facts">
        <Stack gap="sm">
          <Text size="xs" c="dimmed">
            Used to check eligibility for each call. If left blank, eligibility can't be checked.
          </Text>
          {kind === "organizational_unit" && orgType}
          {country}
          {applyingAs}
        </Stack>
      </Fieldset>
    );
  }
  const rest = part === "rest";
  return (
    <>
      {kind === "organizational_unit" && (
        <Fieldset disabled={disabled} mb="sm">
          <Stack gap="sm">
            {!rest && orgType}
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
        {!rest && country}
        {!rest && applyingAs}
        <MultiSelect label="Funding sought" disabled={disabled} data={[
          { value: "project_grant", label: "Project grants" }, { value: "fellowship", label: "Fellowships" },
          { value: "mobility", label: "Research exchanges" }, { value: "infrastructure", label: "Infrastructure" }, { value: "programme", label: "Programmes" },
        ]} value={values.funding_instruments.split("\n").filter(Boolean)} onChange={selected => onChange("funding_instruments", selected.join("\n"))} />
        <Textarea label="Funder exclusions" description="(one per line)" rows={2} value={values.funder_exclusions} onChange={set("funder_exclusions")} />
        {!hideAreas && <Textarea label="Research areas" description="(one per line)" rows={3} value={values.areas} onChange={set("areas")} />}
      </Stack>
    </>
  );
}
