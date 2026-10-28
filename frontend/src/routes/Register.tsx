import { useState } from "react";
import { Link } from "react-router-dom";
import { Alert, Anchor, Button, Stack, Text, TextInput, Title } from "@mantine/core";

import { useAuthConfig, useRegister } from "../api/auth";
import { ApiError } from "../api/client";

export function Register() {
  const cfg = useAuthConfig().data;
  const domains = cfg?.allowed_domains ?? [];
  const register = useRegister();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");

  if (register.isSuccess) {
    return (
      <>
        <Title order={1} size="h4" ta="center">
          Request received
        </Title>
        <Text ta="center" c="dimmed" size="sm">
          Thanks — your access request has been recorded. An administrator will
          review it and set up your account. You'll be able to sign in once it's
          approved.
        </Text>
        <Text ta="center" size="sm">
          <Anchor component={Link} to="/login">
            Back to sign in
          </Anchor>
        </Text>
      </>
    );
  }

  return (
    <>
      <Title order={1} size="h4" ta="center">
        Request access
      </Title>
      <Text ta="center" c="dimmed" size="sm">
        Enter your hospital email. An administrator will review your request and set
        up your account.
      </Text>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          register.mutate({ email, name: name.trim() || null });
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
            placeholder={`you@${domains[0] ?? "example.org"}`}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <TextInput
            label="Name (optional)"
            type="text"
            maxLength={200}
            autoComplete="name"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
          {domains.length > 0 && (
            <Text size="xs" c="dimmed">
              Use your hospital email ({domains.join(" or ")}).
            </Text>
          )}
          {/* A FAILED REQUEST USED TO BE SILENT (review 2026-10-26, A8): on a
              500 the measurement was `alerts=[]`, and the button just
              re-enabled. The success screen is deliberately indistinguishable
              from a rejected address (the server's always-200 is anti-
              enumeration), so silence after a submit reads as success — and the
              member waits for an approval that was never recorded. */}
          {register.isError && (
            <Alert role="alert" color="red" variant="light">
              {/* The generic sentence ALWAYS, the server's own detail after it:
                  apiErrorMessage alone returns only the server's sentence, and
                  a bare "the mailer is down" never says the request was not
                  recorded. */}
              Couldn&apos;t send your request — it has not been recorded. Please try again.
              {register.error instanceof ApiError && ` (${register.error.userMessage})`}
            </Alert>
          )}
          <Button type="submit" fullWidth disabled={register.isPending}>
            Request access
          </Button>
        </Stack>
      </form>
      <Text ta="center" size="sm">
        <Anchor component={Link} to="/login">
          Back to sign in
        </Anchor>
      </Text>
    </>
  );
}
