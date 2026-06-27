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
import styles from "./AdminUsers.module.css";

function UserRow({ u }: { u: AdminUser }) {
  const [pw, setPw] = useState("");
  const disable = useDisableUser();
  const enable = useEnableUser();
  const role = useChangeRole();
  const reset = useResetPassword();
  const busy = disable.isPending || enable.isPending || role.isPending || reset.isPending;
  const err = disable.error || enable.error || role.error || reset.error;
  return (
    <tr>
      <td>{u.email}</td>
      <td>{u.status}</td>
      <td>{u.is_admin ? "admin" : "member"}</td>
      <td className={styles.actions}>
        {u.status === "active" && (
          <>
            <button
              type="button"
              className={styles.secondary}
              disabled={busy}
              onClick={() => disable.mutate({ id: u.id })}
            >
              Disable
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => role.mutate({ id: u.id, role: u.is_admin ? "member" : "admin" })}
            >
              {u.is_admin ? "Make member" : "Make admin"}
            </button>
            <form
              className={styles.resetForm}
              onSubmit={(e) => {
                e.preventDefault();
                reset.mutate({ id: u.id, new_password: pw }, { onSuccess: () => setPw("") });
              }}
            >
              <input
                type="password"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                placeholder="new password"
                required
              />
              <button type="submit" className={styles.secondary} disabled={busy}>
                Reset password
              </button>
            </form>
          </>
        )}
        {u.status === "disabled" && (
          <button type="button" disabled={busy} onClick={() => enable.mutate({ id: u.id })}>
            Enable
          </button>
        )}
        {err && (
          <span role="alert" className={styles.err}>
            {(err as Error).message}
          </span>
        )}
      </td>
    </tr>
  );
}

export function AdminUsers() {
  const [page, setPage] = useState(1);
  const { data, isLoading, isError } = useAdminUsers(page);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load users.</p>;
  const { users, per_page, total } = data;
  return (
    <main className={styles.page}>
      <h1>Users</h1>
      <table className={styles.table}>
        <thead>
          <tr>
            <th>Email</th>
            <th>Status</th>
            <th>Role</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {users.map((u) => (
            <UserRow key={u.id} u={u} />
          ))}
        </tbody>
      </table>
      <p className={styles.pager}>
        Showing page {page} ({total} total).{" "}
        {page > 1 && (
          <button type="button" onClick={() => setPage(page - 1)}>
            Prev
          </button>
        )}
        {total > page * per_page && (
          <button type="button" onClick={() => setPage(page + 1)}>
            Next
          </button>
        )}
      </p>
      <p className={styles.nav}>
        <Link to="/admin/users/pending">Pending approvals</Link>
        {" · "}
        <Link to="/admin">Admin home</Link>
      </p>
    </main>
  );
}
