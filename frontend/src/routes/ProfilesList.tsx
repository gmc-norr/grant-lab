import { Anchor, Badge, Box, Button, Card, Group, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import { useProfilesList } from "../api/profiles";

/** "organizational_unit" -> "Organizational unit": the wire value is an enum
 *  name, not a word a researcher should be shown. */
function kindLabel(kind: string): string {
  const words = kind.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function ProfilesList() {
  const { data, isLoading, isError } = useProfilesList();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" c="red" role="alert">
        Couldn't load profiles.
      </Text>
    );
  const profiles = data.profiles;
  return (
    <Box component="main" maw="56rem" mx="auto" px="xs" pt="md" pb="xl">
      <Group justify="space-between" align="flex-start" wrap="nowrap" gap="md">
        <div>
          <Title order={1}>Ranking profiles</Title>
          <Text c="dimmed" size="sm">
            Each profile describes who you are and what funding you look for. Every call is scored
            against its description and rubric.
          </Text>
        </div>
        {profiles.length > 0 && (
          // flexShrink 0: in a nowrap Group beside a long paragraph the button
          // was squeezed narrower than its label, which then rendered cut off.
          <Button component={Link} to="/profiles/new" style={{ whiteSpace: "nowrap", flexShrink: 0 }}>
            + New profile
          </Button>
        )}
      </Group>
      {profiles.length === 0 ? (
        <Stack align="center" gap="xs" py="xl" c="dimmed">
          <Title order={3}>No profiles yet</Title>
          <Text>Create one to see ranked calls on the dashboard.</Text>
          <Button component={Link} to="/profiles/new" mt="xs">
            Create your first profile
          </Button>
        </Stack>
      ) : (
        <Stack gap="sm" mt="lg">
          {profiles.map((p) => (
            <Card key={p.id} withBorder radius="md" p="md" bg="var(--app-sidebar)">
              <Group gap="xs" wrap="nowrap" align="center">
                <Title order={3} size="h5">
                  <Anchor component={Link} to={`/profiles/${p.id}`}>
                    {p.name}
                  </Anchor>
                </Title>
                {/* Whose it is, said once per card (UX review 2026-10-31): the
                    list never said, and a shared profile looked like your own. */}
                <Badge size="xs" variant="light" color={p.is_owner ? "teal" : "gray"} data-field="ownership">
                  {p.is_owner ? "Owner" : "Shared"}
                </Badge>
              </Group>
              <Text c="dimmed" size="xs">
                {kindLabel(p.kind)}
                {p.areas_count > 0 && ` · ${p.areas_count} areas`}
                {p.budget_currency && ` · ${p.budget_currency}`}
              </Text>
              {p.description && (
                <Text size="sm" mt="xs">
                  {p.description.slice(0, 160)}
                  {p.description.length > 160 ? "…" : ""}
                </Text>
              )}
              <Text size="sm" mt="xs">
                <Anchor component={Link} to={`/?profile=${p.id}`}>
                  Ranked feed →
                </Anchor>
                {/* Only where it works: for anyone else the edit page is a
                    disabled form (same rule as the detail page's Edit). */}
                {p.can_edit && (
                  <>
                    {" · "}
                    <Anchor component={Link} to={`/profiles/${p.id}/edit`}>
                      Edit
                    </Anchor>
                  </>
                )}
              </Text>
            </Card>
          ))}
        </Stack>
      )}
    </Box>
  );
}
