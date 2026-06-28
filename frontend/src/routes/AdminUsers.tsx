import { Anchor, Box, Button, Group, PasswordInput, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminUser } from "../api/admin";
import {
  useAdminUsers,
  useChangeRole,
  useDisableUser,
  useEnableUser,
  useResetPassword,
} from "../api/admin";

function UserRow({ u }: { u: AdminUser }) {
  const [pw, setPw] = useState("");
  const disable = useDisableUser();
  const enable = useEnableUser();
  const role = useChangeRole();
  const reset = useResetPassword();
  const busy = disable.isPending || enable.isPending || role.isPending || reset.isPending;
  const err = disable.error || enable.error || role.error || reset.error;
  return (
    <Table.Tr>
      <Table.Td>{u.email}</Table.Td>
      <Table.Td>{u.status}</Table.Td>
      <Table.Td>{u.is_admin ? "admin" : "member"}</Table.Td>
      <Table.Td>
        <Group gap="xs" align="center" wrap="wrap">
          {u.status === "active" && (
            <>
              <Button size="xs" variant="default" disabled={busy} onClick={() => disable.mutate({ id: u.id })}>
                Disable
              </Button>
              <Button size="xs" disabled={busy}
                      onClick={() => role.mutate({ id: u.id, role: u.is_admin ? "member" : "admin" })}>
                {u.is_admin ? "Make member" : "Make admin"}
              </Button>
              <Box
                component="form"
                onSubmit={(e) => {
                  e.preventDefault();
                  reset.mutate({ id: u.id, new_password: pw }, { onSuccess: () => setPw("") });
                }}
              >
                <Group gap="xs" align="center" wrap="nowrap">
                  <PasswordInput size="xs" w={160} value={pw} onChange={(e) => setPw(e.currentTarget.value)}
                                 placeholder="new password" required />
                  <Button size="xs" variant="default" type="submit" disabled={busy}>
                    Reset password
                  </Button>
                </Group>
              </Box>
            </>
          )}
          {u.status === "disabled" && (
            <Button size="xs" disabled={busy} onClick={() => enable.mutate({ id: u.id })}>
              Enable
            </Button>
          )}
          {err && (
            <Text role="alert" c="red" size="xs">
              {(err as Error).message}
            </Text>
          )}
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}

export function AdminUsers() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminUsers(page);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" role="alert">
        Couldn't load users.
      </Text>
    );
  const { users, per_page, total } = data;
  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Users</Title>
      <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Email</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Role</Table.Th>
            <Table.Th>Actions</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {users.map((u) => (
            <UserRow key={u.id} u={u} />
          ))}
        </Table.Tbody>
      </Table>
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
      <Text size="sm" mt="md">
        <Anchor component={Link} to="/admin/users/pending">Pending approvals</Anchor>
        {" · "}
        <Anchor component={Link} to="/admin">Admin home</Anchor>
      </Text>
    </Box>
  );
}
