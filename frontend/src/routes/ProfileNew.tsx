import { Anchor, Box, Button, Group, NativeSelect, Stack, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useCreateProfile, type ProfileCreateBody } from "../api/profiles";
import { StructuredFields, type StructuredValues } from "./StructuredFields";
import { isMatchEdition } from "../edition";
import { ProfileOptions } from "./ProfileOptions";

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
    <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/profiles" size="sm">
        ← All profiles
      </Anchor>
      <Title order={1} mt="xs">Create a profile</Title>
      <Text c="dimmed" size="sm" mb="md">
        {isMatchEdition ? "Your research interests and funding preferences" : "Pick a kind, add seed data, and the local LLM drafts a description plus rubric. You can edit before it goes live."}
      </Text>
      <form onSubmit={submit}>
        <Stack gap="sm">
          <TextInput label="Profile name" required maxLength={200} value={name}
                     placeholder="e.g. Digital Pathology Lab" onChange={(e) => setName(e.currentTarget.value)} />
          <NativeSelect
            label={isMatchEdition ? "Profile type" : "Kind"}
            data={[
              { value: "organizational_unit", label: isMatchEdition ? "Research group" : "Organizational unit" },
              { value: "individual", label: "Individual researcher" },
            ]}
            value={kind}
            onChange={(e) => setKind(e.currentTarget.value)}
          />
          {(kind === "organizational_unit" || isMatchEdition) && (
            <>
              {!isMatchEdition && <NativeSelect label="Level" data={LEVELS.map(([value, label]) => ({ value, label }))}
                            value={level} onChange={(e) => setLevel(e.currentTarget.value)} />}
              <Textarea label={isMatchEdition ? "What do you research?" : "Mission / strategy / capabilities"} rows={4}
                        value={missionText} onChange={(e) => setMissionText(e.currentTarget.value)} />
            </>
          )}
          {isMatchEdition && <Textarea label="Research areas" description="Optional keywords, one per line — e.g. digital pathology" rows={2} value={struct.areas} onChange={event => setS("areas", event.currentTarget.value)} />}
          {/* MATCH: the three facts that decide eligibility are asked up front;
              folded, they were skipped and every score capped at 50 (UX review
              2026-10-31). Everything else stays optional and folded. */}
          {isMatchEdition ? (
            <>
              <StructuredFields kind={kind} values={struct} onChange={setS} part="eligibility" />
              <ProfileOptions title="Additional preferences (optional)">
                <StructuredFields kind={kind} values={struct} onChange={setS} hideAreas part="rest" />
              </ProfileOptions>
            </>
          ) : (
            <ProfileOptions>
              <StructuredFields kind={kind} values={struct} onChange={setS} hideAreas={isMatchEdition} />
            </ProfileOptions>
          )}
          <ProfileOptions title="Publications and previous grants">
          <Textarea label="Member ORCIDs" description="(one per line; for an individual, your own)" rows={2}
                    value={orcids} placeholder="0000-0001-2345-6789" onChange={(e) => setOrcids(e.currentTarget.value)} />
          <Textarea label="Members by name" description="(one per line: Last F | Affiliation)" rows={2}
                    value={namesAffiliations} onChange={(e) => setNamesAffiliations(e.currentTarget.value)} />
          <Textarea label="Pasted past grants" description="(optional, titles + abstracts)" rows={4}
                    value={pastedGrants} onChange={(e) => setPastedGrants(e.currentTarget.value)} />
          </ProfileOptions>
          <Group gap="sm" align="center">
            <Button type="submit" disabled={create.isPending}>{isMatchEdition ? "Create profile" : "Bootstrap profile"}</Button>
            {create.isError && (
              <Text role="alert" c="red" size="sm">
                {(create.error as Error | null)?.message ?? "Couldn't create the profile."}
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </Box>
  );
}
