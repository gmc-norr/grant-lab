import { Anchor, Box, Button, Fieldset, Group, NativeSelect, Paper, Stack, Text, Textarea, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useProfile, useUpdateProfile, type ProfileDetail, type ProfileUpdateBody } from "../api/profiles";
import { EligibilityWarning } from "./EligibilityWarning";
import { StructuredFields, type StructuredValues } from "./StructuredFields";
import { isMatchEdition } from "../edition";
import { ProfileOptions } from "./ProfileOptions";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];
const CURRENCIES = ["", "SEK", "EUR", "USD", "NOK", "DKK"];

function joinLines(xs: string[]) { return xs.join("\n"); }

export function ProfileEdit() {
  const { id } = useParams();
  const pid = Number(id);
  const { data, isLoading, isError, error } = useProfile(pid);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    // /profiles/{id}/edit calls the SAME useProfile as the detail page and
    // inherits the same two failures. A 404 is honestly ambiguous — unreadable or
    // absent, the API refuses to say which — but anything else is the server
    // failing, and must not be reported as a sharing decision. Same idiom as
    // CallDetail.tsx / ProfileDetail.tsx.
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return (
      <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">
          {notFound
            ? "This profile isn't shared with you, or it doesn't exist."
            : "Couldn't load this profile."}
        </Text>
      </Box>
    );
  }
  return <EditForm key={data.bootstrap_status ?? "ready"} p={data} pid={pid} />;
}

function EditForm({ p, pid }: { p: ProfileDetail; pid: number }) {
  const navigate = useNavigate();
  const update = useUpdateProfile(pid);
  const drafting = p.bootstrap_status === "queued" || p.bootstrap_status === "running";
  const locked = drafting || !p.can_edit;

  const [description, setDescription] = useState(p.description);
  const [level, setLevel] = useState(p.level ?? "");
  const [budgetMin, setBudgetMin] = useState(p.budget_min?.toString() ?? "");
  const [budgetMax, setBudgetMax] = useState(p.budget_max?.toString() ?? "");
  const [currency, setCurrency] = useState(p.budget_currency ?? "");
  const [trlMin, setTrlMin] = useState(p.trl_min?.toString() ?? "");
  const [trlMax, setTrlMax] = useState(p.trl_max?.toString() ?? "");
  const [mustInclude, setMustInclude] = useState(joinLines(p.must_include));
  const [hardExclusions, setHardExclusions] = useState(joinLines(p.hard_exclusions));
  const [struct, setStruct] = useState<StructuredValues>({
    areas: joinLines(p.areas), career_stage: p.career_stage ?? "", applicant_types: joinLines(p.applicant_types),
    role: p.role ?? "", years_since_phd: p.years_since_phd?.toString() ?? "", org_type: p.org_type ?? "",
    capabilities: joinLines(p.capabilities), patient_populations: joinLines(p.patient_populations),
    scale: p.scale ?? "", funding_instruments: joinLines(p.funding_instruments),
    funder_exclusions: joinLines(p.funder_exclusions), country: p.country ?? "", host_institution: p.host_institution ?? "",
  });
  const setS = (k: keyof StructuredValues, v: string) => setStruct((s) => ({ ...s, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileUpdateBody = {
      description, level, budget_min: budgetMin, budget_max: budgetMax, budget_currency: currency,
      trl_min: trlMin, trl_max: trlMax, must_include: mustInclude, hard_exclusions: hardExclusions, ...struct,
    };
    update.mutate(body, { onSuccess: () => navigate(`/profiles/${pid}`) });
  }

  return (
    <Box component="main" maw="44rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to={`/profiles/${pid}`} size="sm">
        ← Back to profile
      </Anchor>
      <Title order={1} mt="xs">Edit {p.name}</Title>
      <Text c="dimmed" size="sm" mb="md">
        {isMatchEdition ? "Research interests and funding preferences" : "Description feeds the semantic similarity gate. Areas, budgets, TRL, must-include and exclusions feed the deterministic rubric gates. Saving kicks off a backfill of the last 90 days against this profile."}
      </Text>
      {drafting && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)">
          <Text size="sm">⏳ Drafting your profile from PubMed and past grants — this can take a minute on the local model.</Text>
        </Paper>
      )}
      {p.bootstrap_status === "done" && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)">
          <Text size="sm">
            Draft ready.
            {p.bootstrap_pubmed_status === "ok" && ` Found ${p.publications_fetched} publications for your members.`}
            {p.bootstrap_pubmed_status === "empty" && " No publications matched your members on PubMed — the draft rests on your mission text and pasted grants."}
            {p.bootstrap_pubmed_status === "unavailable" && " PubMed was unavailable, so the draft rests on your mission text and pasted grants."}
          </Text>
        </Paper>
      )}
      {p.bootstrap_status === "failed" && (
        <Paper withBorder radius="md" p="sm" mb="md" bg="var(--app-sidebar)" style={{ borderColor: "var(--app-warn, var(--app-bad))" }}>
          <Text size="sm">We couldn't auto-draft a description (the local model was unavailable). Write one below and save.</Text>
        </Paper>
      )}
      {!p.can_edit && (
        // A disabled form with no reason given read as broken (UX review
        // 2026-10-31). The list and detail pages no longer link here for a
        // reader who cannot edit, but a shared or typed link still can.
        <Text size="sm" c="dimmed" mb="md" data-field="read-only">
          You can view this profile, but only its owner or an admin can change it.
        </Text>
      )}
      {/* Off the LIVE form state, not `p`: the warning has to clear the moment a
          fact is filled in, and it must sit outside ProfileOptions, which folds
          its children into a closed <details> in the match edition. */}
      <EligibilityWarning
        applicantTypes={struct.applicant_types.split("\n").filter(Boolean)}
        orgType={struct.org_type}
        country={struct.country}
      />
      <form onSubmit={submit}>
        <Fieldset disabled={locked}>
          <Stack gap="sm">
            <Textarea label="Description" description={isMatchEdition ? undefined : "Changing this triggers a background re-embed."} rows={6}
                      value={description} onChange={(e) => setDescription(e.currentTarget.value)} />
            {isMatchEdition && <Textarea label="Research areas" rows={3} value={struct.areas} onChange={event => setS("areas", event.currentTarget.value)} />}
            {/* MATCH: eligibility up front, as on the create page (UX review
                2026-10-31); the warning above names exactly these fields. */}
            {isMatchEdition && (
              <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} part="eligibility" />
            )}
            <ProfileOptions title={isMatchEdition ? "More preferences (optional)" : undefined}>
            {p.kind === "organizational_unit" && (
              <NativeSelect label="Level" data={LEVELS.map(([value, label]) => ({ value, label }))}
                            value={level} onChange={(e) => setLevel(e.currentTarget.value)} />
            )}
            <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} hideAreas={isMatchEdition} part={isMatchEdition ? "rest" : "all"} />
            <TextInput label="Budget min" value={budgetMin} onChange={(e) => setBudgetMin(e.currentTarget.value)} />
            <TextInput label="Budget max" value={budgetMax} onChange={(e) => setBudgetMax(e.currentTarget.value)} />
            <NativeSelect label="Currency" data={CURRENCIES.map((c) => ({ value: c, label: c || "(unknown)" }))}
                          value={currency} onChange={(e) => setCurrency(e.currentTarget.value)} />
            <TextInput label="Technology Readiness Level — min" description="(1–9)" value={trlMin} onChange={(e) => setTrlMin(e.currentTarget.value)} />
            <TextInput label="Technology Readiness Level — max" description="(1–9)" value={trlMax} onChange={(e) => setTrlMax(e.currentTarget.value)} />
            <Textarea label="Must-include keywords" description={isMatchEdition ? undefined : "(one per line; skips the topic-similarity check — won't rescue a call whose deadline has passed)"} rows={3}
                      value={mustInclude} onChange={(e) => setMustInclude(e.currentTarget.value)} />
            <Textarea label="Hard exclusions" description="(one per line)" rows={3}
                      value={hardExclusions} onChange={(e) => setHardExclusions(e.currentTarget.value)} />
            </ProfileOptions>
            <Group gap="sm" align="center">
              <Button type="submit" disabled={locked || update.isPending}>{isMatchEdition ? "Save preferences" : "Save and re-rank"}</Button>
              <Anchor component={Link} to={`/profiles/${pid}`} size="sm">Cancel</Anchor>
              {update.isError && (
                <Text role="alert" c="red" size="sm">
                  {(update.error as Error | null)?.message ?? "Couldn't save."}
                </Text>
              )}
            </Group>
          </Stack>
        </Fieldset>
      </form>
    </Box>
  );
}
