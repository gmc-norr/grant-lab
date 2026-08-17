import { Anchor, Box, Code, Table, Text, Title } from "@mantine/core";
import { Link, useParams } from "react-router-dom";

import { useAdminDeletedWorkspace } from "../api/admin";

const fmtDt = (s: string) => new Date(s).toLocaleString();

type Props = { workspaceId?: string };

// Reads the workspace id from the route param (wired at
// /admin/deleted-workspaces/:workspaceId in router.tsx). The prop lets a test
// render this component directly, without a matching <Route>, the way
// renderWithProviders is used elsewhere in this test file.
export function AdminDeletedWorkspaceDetail({ workspaceId: workspaceIdProp }: Props = {}) {
  const { workspaceId: workspaceIdParam } = useParams();
  const workspaceId = workspaceIdProp ?? workspaceIdParam ?? "";
  const { data, isLoading, isError } = useAdminDeletedWorkspace(workspaceId);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load this deleted workspace.</Text>
      </Box>
    );
  const d = data;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Anchor component={Link} to="/admin/deleted-workspaces" size="sm">
        ← Deleted workspaces
      </Anchor>
      <Title order={1} mt="xs">
        {d.title}
      </Title>
      <Text c="dimmed" size="sm">
        Owner: {d.owner_email ?? "—"} · Deleted {fmtDt(d.soft_deleted_at)}
      </Text>

      <Title order={2} size="h5" mt="md" mb="sm">
        Proposal draft (at HEAD)
      </Title>
      {!d.repository_available ? (
        <Text c="dimmed">This workspace has no readable repository.</Text>
      ) : d.proposal_markdown == null ? (
        <Text c="dimmed">No proposal draft was found at HEAD.</Text>
      ) : (
        <Code block fz="xs" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {d.proposal_markdown}
        </Code>
      )}

      <Title order={2} size="h5" mt="md" mb="sm">
        Commit history
      </Title>
      {d.history_truncated && (
        <Text c="dimmed" size="sm" mb="xs">
          Showing only the most recent {d.history_limit} commits.
        </Text>
      )}
      {/* An unreadable history is not an empty one. Saying "No commits." when
          git log failed tells an admin checking what survived that the history
          is gone — and if a draft rendered above, it provably is not. */}
      {!d.history_available ? (
        <Text c="dimmed">
          The commit history could not be read. This does not mean it is gone — nothing on
          disk is removed when a workspace is deleted.
        </Text>
      ) : d.commits.length === 0 ? (
        <Text c="dimmed">No commits.</Text>
      ) : (
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Commit</Table.Th>
              <Table.Th>Authored</Table.Th>
              <Table.Th>Subject</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {d.commits.map((c) => (
              <Table.Tr key={c.sha}>
                <Table.Td>
                  <Code>{c.sha.slice(0, 8)}</Code>
                </Table.Td>
                <Table.Td>{fmtDt(c.authored_at)}</Table.Td>
                <Table.Td>{c.subject}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Box>
  );
}
