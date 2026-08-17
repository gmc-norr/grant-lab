import { Anchor, Box, Button, Group, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { DeletedWorkspaceRow } from "../api/admin";
import { DELETED_PER_PAGE, useAdminDeletedWorkspaces, useRestoreWorkspace } from "../api/admin";
import { useSession } from "../api/session";

const fmtDt = (s: string) => new Date(s).toLocaleString();

function Row({ w, canInspect }: { w: DeletedWorkspaceRow; canInspect: boolean }) {
  const restore = useRestoreWorkspace(w.id);
  return (
    <>
      <Table.Tr>
        <Table.Td>
          {/* No link when reading content is disabled: the detail route 403s, and
              a door that refuses is worse than no door. The row itself stays —
              seeing WHAT was deleted is the half of this surface that is never
              gated. */}
          {canInspect ? (
            <Anchor component={Link} to={`/admin/deleted-workspaces/${w.id}`}>
              {w.title}
            </Anchor>
          ) : (
            w.title
          )}
        </Table.Td>
        <Table.Td>{w.owner_email ?? "—"}</Table.Td>
        <Table.Td>{fmtDt(w.soft_deleted_at)}</Table.Td>
        <Table.Td>
          <Button
            size="xs"
            variant="outline"
            disabled={restore.isPending}
            onClick={async () => {
              if (
                window.confirm(
                  `Restore "${w.title}"? The workspace will become active again and the owner will regain access.`,
                )
              ) {
                restore.mutate();
              }
            }}
          >
            Restore
          </Button>
        </Table.Td>
      </Table.Tr>
      {restore.isError && (
        <Table.Tr>
          <Table.Td colSpan={4}>
            <Text c="red" size="sm" role="alert">
              Could not restore workspace — the server returned an error. Please try again.
            </Text>
          </Table.Td>
        </Table.Tr>
      )}
    </>
  );
}

export function AdminDeletedWorkspaces() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminDeletedWorkspaces(page);
  const session = useSession();
  const canInspect = session.data?.capabilities.retention_inspection === true;
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" role="alert">
        Couldn't load deleted workspaces.
      </Text>
    );
  const { items, total } = data;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Deleted workspaces</Title>
      <Text c="dimmed" size="sm" mb="md">
        Deletion freezes a workspace but keeps its content on disk. The content
        cannot be edited here — restore is the only action available.
      </Text>
      {!canInspect && (
        <Text c="dimmed" size="sm" mb="md">
          Reading a deleted workspace's draft is disabled on this deployment, so
          titles here do not open. An administrator can enable it with{" "}
          <Text span ff="monospace">
            RETENTION_INSPECTION_ENABLED
          </Text>
          .
        </Text>
      )}
      {items.length === 0 ? (
        <Text c="dimmed">No deleted workspaces.</Text>
      ) : (
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Title</Table.Th>
              <Table.Th>Owner</Table.Th>
              <Table.Th>Deleted</Table.Th>
              <Table.Th>Actions</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {items.map((w) => (
              <Row key={w.id} w={w} canInspect={canInspect} />
            ))}
          </Table.Tbody>
        </Table>
      )}
      <Group gap="xs" align="center" mt="md">
        <Text c="dimmed" size="sm">Showing page {page} ({total} total).</Text>
        {page > 1 && (
          <Button size="xs" variant="default" onClick={() => setPage(page - 1)}>
            Prev
          </Button>
        )}
        {total > page * DELETED_PER_PAGE && (
          <Button size="xs" variant="default" onClick={() => setPage(page + 1)}>
            Next
          </Button>
        )}
      </Group>
      <Text size="sm" mt="md">
        <Anchor component={Link} to="/admin">Admin home</Anchor>
      </Text>
    </Box>
  );
}
