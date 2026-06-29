import { Anchor, Badge, Box, Group, Text, Title } from "@mantine/core";
import { Link, useSearchParams } from "react-router-dom";

import type { components } from "../api/schema.gen";
import { useWorkspaces } from "../api/workspace";
import { Deadline } from "./Deadline";

type WsItem = components["schemas"]["WorkspaceListItemOut"];

const STATUS_LABEL: Record<string, string> = {
  drafting: "Drafting",
  submitted: "Submitted",
  awarded: "Awarded",
  rejected: "Rejected",
  abandoned: "Abandoned",
};

function WorkspaceRow({ ws }: { ws: WsItem }) {
  // Both setup- and active-phase workspaces open in the React cockpit (Slice E
  // brought the setup + rubric flow in-cockpit). The "Setting up" badge marks setup.
  const setup = ws.current_phase === "setup";
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Box miw={0}>
        <Anchor component={Link} fw={500} to={`/cockpit/${ws.id}`}>
          {ws.title}
        </Anchor>
        <Group mt="0.15rem" gap="0.4rem" align="center" c="dimmed" fz="0.85rem">
          <Text span tt="uppercase" fz="0.72rem" c="var(--app-link)">{STATUS_LABEL[ws.status] ?? ws.status}</Text>
          {ws.linked_call_title && (
            <>
              <Text span aria-hidden> · </Text>
              <Text span>{ws.linked_call_title}</Text>
            </>
          )}
          {ws.deadline && (
            <>
              <Text span aria-hidden> · </Text>
              <Deadline iso={ws.deadline} />
            </>
          )}
          <Text span aria-hidden> · </Text>
          {/* updated_at is an ISO string; slice to YYYY-MM-DD for a stable label. */}
          <Text span>Updated {ws.updated_at.slice(0, 10)}</Text>
          {!ws.is_owner && <Badge variant="default" size="xs">Shared</Badge>}
          {setup && <Badge variant="default" size="xs">Setting up</Badge>}
        </Group>
      </Box>
    </Group>
  );
}

export function WorkspaceList() {
  const [sp] = useSearchParams();
  const status = sp.get("status");
  const { data, isLoading, isError } = useWorkspaces(status);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError) return <Text role="alert" p="md">Couldn&apos;t load workspaces.</Text>;

  const items = data ?? [];
  return (
    <Box component="main">
      <Group justify="space-between" align="center" gap="sm">
        <Title order={1} fz="1.1rem">Workspaces</Title>
        <Anchor component={Link} fw={500} fz="0.9rem" to="/">
          New workspace
        </Anchor>
      </Group>
      {items.length === 0 ? (
        <Text c="dimmed" my="md">
          No workspaces yet — <Anchor component={Link} to="/">pick a call to start drafting</Anchor>.
        </Text>
      ) : (
        <Box component="ul" p={0} my="xs">
          {items.map((ws) => (
            <WorkspaceRow key={ws.id} ws={ws} />
          ))}
        </Box>
      )}
    </Box>
  );
}
