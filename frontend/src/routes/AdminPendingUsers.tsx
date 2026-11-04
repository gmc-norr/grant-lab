import { Anchor, Box, Button, Card, Group, PasswordInput, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminPendingUser } from "../api/admin";
import { useAdminPendingUsers, useApproveUser, useDenyUser } from "../api/admin";

function PendingRow({ u }: { u: AdminPendingUser }) {
  const [pw, setPw] = useState("");
  const approve = useApproveUser();
  const deny = useDenyUser();
  const busy = approve.isPending || deny.isPending;
  const err = approve.error || deny.error;
  // Local (non-Google) accounts need an admin-issued password to log in.
  const needsPw = !u.google_linked;
  const doApprove = (role: "member" | "admin") =>
    approve.mutate({ id: u.id, role, initial_password: needsPw ? pw : undefined });
  return (
    <Card component="article" withBorder radius="md" p="md" my="sm" bg="var(--app-sidebar)">
      <Text mb="sm">
        <Text span fw={700}>{u.email}</Text>
        {u.name ? ` — ${u.name}` : ""}
        {u.google_linked && <Text span c="dimmed" size="sm"> (Google)</Text>}
        <Text span c="dimmed" size="sm"> · registered {new Date(u.created_at).toLocaleDateString()}</Text>
      </Text>
      {needsPw && (
        <PasswordInput
          size="xs"
          value={pw}
          onChange={(e) => setPw(e.currentTarget.value)}
          placeholder="initial password"
          required
          mb="sm"
        />
      )}
      <Group gap="xs" wrap="wrap">
        <Button size="xs" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("member")}>
          Approve as member
        </Button>
        <Button size="xs" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("admin")}>
          Approve as admin
        </Button>
        <Button size="xs" variant="default" disabled={busy} onClick={() => deny.mutate({ id: u.id })}>
          Deny
        </Button>
      </Group>
      {err && (
        <Text role="alert" c="red" size="xs" mt="xs">
          {(err as Error).message}
        </Text>
      )}
    </Card>
  );
}

export function AdminPendingUsers() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminPendingUsers(page);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" role="alert">
        Couldn't load pending accounts.
      </Text>
    );
  const { users, per_page, total } = data;
  return (
    <Box component="main" maw="48rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Pending accounts</Title>
      {users.length === 0 && <Text>No accounts awaiting approval.</Text>}
      {users.map((u) => (
        <PendingRow key={u.id} u={u} />
      ))}
      {total > 0 && (
        <Group gap="xs" align="center" mt="md">
          <Text c="dimmed" size="sm">Showing page {page} ({total} total).</Text>
          {page > 1 && (
            <Button size="xs" variant="default" onClick={() => setPage(page - 1)}>
              Prev
            </Button>
          )}
          {total > page * per_page && (
            <Button size="xs" variant="default" onClick={() => setPage(page + 1)}>
              Next
            </Button>
          )}
        </Group>
      )}
      <Text size="sm" mt="md">
        <Anchor component={Link} to="/admin/users">All users</Anchor>
        {" · "}
        <Anchor component={Link} to="/admin">Admin home</Anchor>
      </Text>
    </Box>
  );
}
