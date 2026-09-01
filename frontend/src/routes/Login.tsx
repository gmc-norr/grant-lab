import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Alert, Anchor, Button, PasswordInput, Stack, Text, TextInput, Title } from "@mantine/core";

import { useAuthConfig, useLogin } from "../api/auth";
import { apiErrorMessage } from "../api/client";

function errorMessage(err: unknown): string {
  return apiErrorMessage(err, "Something went wrong. Please try again.");
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
      <Title order={1} size="h4" ta="center">
        Sign in
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Enter your email and password.
      </Text>
      {login.isError && <Alert color="red">{errorMessage(login.error)}</Alert>}
      <form
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
        <Stack gap="sm">
          <TextInput
            label="Email"
            type="email"
            required
            autoFocus
            autoComplete="email"
            value={email}
            placeholder={`you@${cfg?.allowed_domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <PasswordInput
            label="Password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.currentTarget.value)}
          />
          <Button type="submit" fullWidth disabled={login.isPending}>
            Sign in
          </Button>
        </Stack>
      </form>
      {cfg?.google_oidc_enabled && (
        // Native server-side OAuth start: a full-page POST carries Origin/CSRF and
        // 303s to Google (cannot be a fetch). safe_next is applied server-side.
        <form method="post" action="/auth/google/start">
          {next && <input type="hidden" name="next" value={next} />}
          <Button type="submit" variant="default" fullWidth>
            Continue with Google
          </Button>
        </form>
      )}
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/register">
          Request access
        </Anchor>
      </Text>
    </>
  );
}
