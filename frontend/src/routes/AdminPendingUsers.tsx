import { useState } from "react";
import { Link } from "react-router-dom";

import type { AdminPendingUser } from "../api/admin";
import { useAdminPendingUsers, useApproveUser, useDenyUser } from "../api/admin";
import styles from "./AdminPendingUsers.module.css";

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
    <article className={styles.card}>
      <p className={styles.head}>
        <strong>{u.email}</strong>
        {u.name ? ` — ${u.name}` : ""}
        {u.google_linked && <small className={styles.tag}> (Google)</small>}
        <small className={styles.tag}> · registered {new Date(u.created_at).toLocaleDateString()}</small>
      </p>
      {needsPw && (
        <input
          type="password"
          value={pw}
          onChange={(e) => setPw(e.target.value)}
          placeholder="initial password"
          required
        />
      )}
      <div className={styles.actions}>
        <button type="button" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("member")}>
          Approve as member
        </button>
        <button type="button" disabled={busy || (needsPw && !pw)} onClick={() => doApprove("admin")}>
          Approve as admin
        </button>
        <button type="button" className={styles.secondary} disabled={busy} onClick={() => deny.mutate({ id: u.id })}>
          Deny
        </button>
      </div>
      {err && (
        <span role="alert" className={styles.err}>
          {(err as Error).message}
        </span>
      )}
    </article>
  );
}

export function AdminPendingUsers() {
  const { data, isLoading, isError } = useAdminPendingUsers(1);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load pending accounts.</p>;
  const users = data.users;
  return (
    <main className={styles.page}>
      <h1>Pending accounts</h1>
      {users.length === 0 && <p>No accounts awaiting approval.</p>}
      {users.map((u) => (
        <PendingRow key={u.id} u={u} />
      ))}
      <p className={styles.nav}>
        <Link to="/admin/users">All users</Link>
        {" · "}
        <Link to="/admin">Admin home</Link>
      </p>
    </main>
  );
}
