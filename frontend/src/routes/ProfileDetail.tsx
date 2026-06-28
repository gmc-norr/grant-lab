import { Anchor, Badge, Box, Button, Card, Group, Text, Title } from "@mantine/core";
import { Link, useParams } from "react-router-dom";

import type { ProfileDetail as Detail } from "../api/profiles";
import { useProfile } from "../api/profiles";

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) return <>—</>;
  return (
    <>
      {items.map((c, i) => (
        <Badge key={i} variant="default" tt="none" radius="xl" size="sm" mr={4} mt={4}>
          {c}
        </Badge>
      ))}
    </>
  );
}

function range(min: number | null, max: number | null) {
  if (min == null && max == null) return "—";
  return `${min ?? "—"} – ${max ?? "—"}`;
}

export function ProfileDetail() {
  const { id } = useParams();
  const { data, isLoading, isError } = useProfile(Number(id));
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text role="alert">Profile not found.</Text>
      </Box>
    );
  return <DetailView p={data} />;
}

function DetailView({ p }: { p: Detail }) {
  const hasProfileFields =
    p.org_type || p.capabilities.length || p.patient_populations.length || p.scale ||
    p.career_stage || p.applicant_types.length || p.role || p.years_since_phd != null ||
    p.funding_instruments.length || p.funder_exclusions.length || p.country || p.host_institution;
  return (
    <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/profiles" size="sm">
        ← All profiles
      </Anchor>
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md" my="md">
        <div>
          <Title order={1}>{p.name}</Title>
          <Text c="dimmed" size="sm">
            {p.kind}
            {p.level ? ` · ${p.level}` : ""}
          </Text>
        </div>
        <Group gap="xs" wrap="nowrap">
          <Button component={Link} to={`/?profile=${p.id}`}>
            Ranked feed →
          </Button>
          <Button component={Link} variant="default" to={`/profiles/${p.id}/edit`}>
            Edit
          </Button>
        </Group>
      </Group>

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
        <Title order={2} size="h5" mb="sm">
          Description
        </Title>
        <Text>{p.description || "— (no description yet)"}</Text>
      </Card>

      <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
        <Title order={2} size="h5" mb="sm">
          Ranking rubric
        </Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Areas</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.areas} /></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Budget</Text>
          <Text component="dd" size="sm" m={0}>
            {p.budget_min == null && p.budget_max == null
              ? "—"
              : `${p.budget_min?.toLocaleString() ?? "—"} – ${p.budget_max?.toLocaleString() ?? "—"}${p.budget_currency ? ` ${p.budget_currency}` : ""}`}
          </Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Technology Readiness Level</Text>
          <Text component="dd" size="sm" m={0}>{range(p.trl_min, p.trl_max)}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Must include</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.must_include} /></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Hard exclusions</Text>
          <Text component="dd" size="sm" m={0}><Chips items={p.hard_exclusions} /></Text>
        </Box>
      </Card>

      {hasProfileFields && (
        <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
          <Title order={2} size="h5" mb="sm">
            Profile
          </Title>
          <Box component="dl" m={0}>
            {p.org_type && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Organization type</Text><Text component="dd" size="sm" m={0}>{p.org_type}</Text></>)}
            {p.capabilities.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Capabilities</Text><Text component="dd" size="sm" m={0}><Chips items={p.capabilities} /></Text></>)}
            {p.patient_populations.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Patient populations</Text><Text component="dd" size="sm" m={0}><Chips items={p.patient_populations} /></Text></>)}
            {p.scale && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Scale</Text><Text component="dd" size="sm" m={0}>{p.scale}</Text></>)}
            {p.career_stage && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Career stage</Text><Text component="dd" size="sm" m={0}>{p.career_stage}</Text></>)}
            {p.role && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Role</Text><Text component="dd" size="sm" m={0}>{p.role}</Text></>)}
            {p.years_since_phd != null && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Years since PhD</Text><Text component="dd" size="sm" m={0}>{p.years_since_phd}</Text></>)}
            {p.applicant_types.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Applicant types</Text><Text component="dd" size="sm" m={0}><Chips items={p.applicant_types} /></Text></>)}
            {p.funding_instruments.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Funding instruments</Text><Text component="dd" size="sm" m={0}><Chips items={p.funding_instruments} /></Text></>)}
            {p.funder_exclusions.length > 0 && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Funder exclusions</Text><Text component="dd" size="sm" m={0}><Chips items={p.funder_exclusions} /></Text></>)}
            {p.host_institution && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Host institution</Text><Text component="dd" size="sm" m={0}>{p.host_institution}</Text></>)}
            {p.country && (<><Text component="dt" size="xs" c="dimmed" mt="xs">Country</Text><Text component="dd" size="sm" m={0}>{p.country}</Text></>)}
          </Box>
        </Card>
      )}
    </Box>
  );
}
