import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import { useAuthConfig, useLogin } from "../api/auth";
import { ApiError } from "../api/client";
import styles from "./PreAuth.module.css";

function errorMessage(err: unknown): string {
  // unwrap throws ApiError(code, "code: message"); strip the code prefix so the
  // form shows the bare server message (always the generic one for a 400).
  if (err instanceof ApiError) return err.message.slice(err.code.length + 2);
  return "Something went wrong. Please try again.";
}

export function Login() {
  const cfg = useAuthConfig().data;
  const [sp] = useSearchParams();
  const next = sp.get("next");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const login = useLogin();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");

  return (
    <>
      <h1>Sign in</h1>
      <p>Enter your email and password.</p>
      {login.isError && (
        <p className={styles.err} role="alert">
          {errorMessage(login.error)}
        </p>
      )}
      <form
        className={styles.actions}
        onSubmit={(e) => {
          e.preventDefault();
          login.mutate(
            { email, password, next },
            {
              onSuccess: (data) => {
                qc.invalidateQueries({ queryKey: ["me"] });
                navigate(data.redirect);
              },
            },
          );
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
            placeholder={`you@${cfg?.allowed_domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          Password
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button type="submit" disabled={login.isPending}>
          Sign in
        </button>
      </form>
      {cfg?.google_oidc_enabled && (
        // Native server-side OAuth start: a full-page POST carries Origin/CSRF and
        // 303s to Google (cannot be a fetch). safe_next is applied server-side.
        <form method="post" action="/auth/google/start" className={styles.actions}>
          {next && <input type="hidden" name="next" value={next} />}
          <button type="submit" className={styles.secondary}>
            Continue with Google
          </button>
        </form>
      )}
      <p className={styles.link}>
        <Link to="/register">Request access</Link>
      </p>
    </>
  );
}
