import { Anchor, Box, Card, Code, Table, Text, Title } from "@mantine/core";
import { Link } from "react-router-dom";

import { useAdminDrafting } from "../api/adminDashboard";

const fmtDt = (s: string) => new Date(s).toLocaleString();

export function AdminDrafting() {
  const { data, isLoading, isError } = useAdminDrafting();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the drafting panel.</Text>
      </Box>
    );
  const d = data;
  const cap = d.global_cap;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Drafting (B7/B8) panel</Title>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Kill switch</Title>
        <Text>{d.drafting_enabled ? <Text span fw={700}>Drafting enabled</Text> : <Text span fw={700}>Drafting disabled</Text>}</Text>
        <Text c="dimmed" size="sm">Toggle via env <Code>DRAFTING_ENABLED=true|false</Code> and restart. UI toggling is a backlog item.</Text>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Global daily cap (today, Europe/Stockholm)</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Dimension</Table.Th><Table.Th>Used / Cap</Table.Th><Table.Th>Status</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            <Table.Tr><Table.Td>Input tokens</Table.Td><Table.Td>{cap.used_in} / {cap.cap_in}</Table.Td>
              <Table.Td>{cap.reason === "input_cap_exceeded" ? <Text span fw={700}>EXCEEDED</Text> : "OK"}</Table.Td></Table.Tr>
            <Table.Tr><Table.Td>Output tokens</Table.Td><Table.Td>{cap.used_out} / {cap.cap_out}</Table.Td>
              <Table.Td>{cap.reason === "output_cap_exceeded" ? <Text span fw={700}>EXCEEDED</Text> : "OK"}</Table.Td></Table.Tr>
          </Table.Tbody>
        </Table>
        <Text mt="sm">{cap.allowed ? "Overall status: OK" : <Text span fw={700}>Overall status: EXCEEDED ({cap.reason})</Text>}</Text>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Per-workspace usage today</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Title</Table.Th><Table.Th>Owner</Table.Th><Table.Th>Input</Table.Th><Table.Th>Output</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {d.per_workspace.length === 0 ? (
              <Table.Tr><Table.Td colSpan={4} c="dimmed">No workspaces.</Table.Td></Table.Tr>
            ) : d.per_workspace.map((w) => (
              <Table.Tr key={w.id}>
                <Table.Td><Anchor component={Link} to={`/cockpit/${w.id}`}>{w.title}</Anchor></Table.Td>
                <Table.Td>{w.owner_email}</Table.Td><Table.Td>{w.used_in}</Table.Td><Table.Td>{w.used_out}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Recent failed / cancelled turns (last 50)</Title>
        {d.recent_events.length === 0 ? (
          <Text c="dimmed" size="sm">No recent failures.</Text>
        ) : (
          <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
            <Table.Thead><Table.Tr><Table.Th>When</Table.Th><Table.Th>Workspace</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Detail</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>
              {d.recent_events.map((e, i) => (
                <Table.Tr key={i}>
                  <Table.Td>{fmtDt(e.created_at)}</Table.Td><Table.Td>{e.workspace_id}</Table.Td>
                  <Table.Td><Code>{e.kind}</Code></Table.Td>
                  <Table.Td><Code block fz="xs" style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{JSON.stringify(e.payload, null, 2)}</Code></Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Box>
  );
}
