import { useState } from "react";
import { Link } from "react-router-dom";

import { useAuthConfig, useRegister } from "../api/auth";
import styles from "./PreAuth.module.css";

export function Register() {
  const cfg = useAuthConfig().data;
  const domains = cfg?.allowed_domains ?? [];
  const register = useRegister();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  if (register.isSuccess) {
    return (
      <>
        <h1>Request received</h1>
        <p>
          Thanks — your access request has been recorded. An administrator will
          review it and set up your account. You'll be able to sign in once it's
          approved.
        </p>
        <p className={styles.link}>
          <Link to="/login">Back to sign in</Link>
        </p>
      </>
    );
  }

  return (
    <>
      <h1>Request access</h1>
      <p>
        Enter your hospital email. An administrator will review your request and set
        up your account.
      </p>
      <form
        className={styles.actions}
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate({ email, name: name.trim() || null });
        }}
      >
        <label className={styles.field}>
          Email
          <input
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          Name (optional)
          <input
            type="text"
            maxLength={200}
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        {domains.length > 0 && (
          <span className={styles.muted}>Use your hospital email ({domains.join(" or ")}).</span>
        )}
        <button type="submit" disabled={register.isPending}>
          Request access
        </button>
      </form>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
