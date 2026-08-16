import { Anchor, Box, Button, Group, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { DeletedWorkspaceRow } from "../api/admin";
import { DELETED_PER_PAGE, useAdminDeletedWorkspaces } from "../api/admin";

const fmtDt = (s: string) => new Date(s).toLocaleString();

function Row({ w }: { w: DeletedWorkspaceRow }) {
  return (
    <Table.Tr>
      <Table.Td>
        <Anchor component={Link} to={`/admin/deleted-workspaces/${w.id}`}>
          {w.title}
        </Anchor>
      </Table.Td>
      <Table.Td>{w.owner_email ?? "—"}</Table.Td>
      <Table.Td>{fmtDt(w.soft_deleted_at)}</Table.Td>
    </Table.Tr>
  );
}

export function AdminDeletedWorkspaces() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminDeletedWorkspaces(page);
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
        Deletion freezes a workspace but keeps its content on disk. This view is
        read-only — nothing here can be restored, changed, or removed.
      </Text>
      {items.length === 0 ? (
        <Text c="dimmed">No deleted workspaces.</Text>
      ) : (
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Title</Table.Th>
              <Table.Th>Owner</Table.Th>
              <Table.Th>Deleted</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {items.map((w) => (
              <Row key={w.id} w={w} />
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
