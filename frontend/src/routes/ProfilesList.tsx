import { Anchor, Box, Button, Card, Group, Stack, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import { useProfilesList } from "../api/profiles";

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
            Each profile defines who you are and what you fund-hunt for. The LLM ranks calls against the
            description and rubric.
          </Text>
        </div>
        {profiles.length > 0 && (
          <Button component={Link} to="/profiles/new" style={{ whiteSpace: "nowrap" }}>
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
              <Title order={3} size="h5">
                <Anchor component={Link} to={`/profiles/${p.id}`}>
                  {p.name}
                </Anchor>
              </Title>
              <Text c="dimmed" size="xs">
                {p.kind}
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
                {" · "}
                <Anchor component={Link} to={`/profiles/${p.id}/edit`}>
                  Edit
                </Anchor>
              </Text>
            </Card>
          ))}
        </Stack>
      )}
    </Box>
  );
}
