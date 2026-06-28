import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  Anchor,
  Button,
  Checkbox,
  Code,
  Group,
  PasswordInput,
  Paper,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";

import type { AccountSettings } from "../api/account";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "../api/account";

export function Settings() {
  const { data, isLoading, isError } = useAccountSettings();
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Text p="md" c="red" role="alert">
        Couldn't load settings.
      </Text>
    );
  return <SettingsForm data={data} />;
}

function SettingsForm({ data }: { data: AccountSettings }) {
  return (
    <Stack component="main" maw="40rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <div>
        <Title order={1}>Settings</Title>
        <Text c="dimmed" size="sm">
          Signed in as <Code>{data.email}</Code>
          {data.is_admin && (
            <>
              {" · "}
              <Text span c="dimmed">
                admin
              </Text>
            </>
          )}
          .
        </Text>
      </div>
      <NameSection initial={data.name ?? ""} />
      {data.google_oidc_enabled && <GoogleSection linked={data.google_linked} />}
      {data.has_password && <PasswordSection />}
      <SubscriptionsSection profiles={data.profiles} />
    </Stack>
  );
}

function SectionCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Paper component="section" withBorder radius="md" p="md" bg="var(--app-sidebar)">
      <Title order={2} size="h4" mb="sm">
        {title}
      </Title>
      {children}
    </Paper>
  );
}

function NameSection({ initial }: { initial: string }) {
  const [name, setName] = useState(initial);
  const update = useUpdateName();
  return (
    <SectionCard title="Display name">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(name);
        }}
      >
        <Stack gap="sm">
          <TextInput
            label="How should we address you?"
            type="text"
            value={name}
            maxLength={200}
            placeholder="e.g. Pär Larsson"
            onChange={(e) => setName(e.currentTarget.value)}
          />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={update.isPending}>
              Save name
            </Button>
            {update.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Saved.
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}

function GoogleSection({ linked }: { linked: boolean }) {
  return (
    <SectionCard title="Google sign-in">
      {linked ? (
        <Text>
          Google sign-in:{" "}
          <Text span fw={700}>
            connected
          </Text>
          .
        </Text>
      ) : (
        // Native server-side OAuth start — full-page POST, not /api. Owned by the auth slice.
        <form method="post" action="/auth/google/start">
          <input type="hidden" name="link" value="1" />
          <Button type="submit">Connect Google account</Button>
        </form>
      )}
    </SectionCard>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useChangePassword();
  return (
    <SectionCard title="Change password">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          change.mutate(
            { current_password: current, new_password: next },
            {
              onSuccess: () => {
                setCurrent("");
                setNext("");
              },
            },
          );
        }}
      >
        <Stack gap="sm">
          <PasswordInput
            label="Current password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.currentTarget.value)}
          />
          <PasswordInput
            label="New password"
            autoComplete="new-password"
            required
            value={next}
            onChange={(e) => setNext(e.currentTarget.value)}
          />
          <Group gap="sm" align="center">
            <Button type="submit" disabled={change.isPending}>
              Change password
            </Button>
            {change.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Password changed.
              </Text>
            )}
            {change.isError && (
              <Text role="alert" c="red" size="sm">
                {(change.error as Error | null)?.message ?? "Couldn't change password."}
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}

function SubscriptionsSection({ profiles }: { profiles: AccountSettings["profiles"] }) {
  const [checked, setChecked] = useState<Set<number>>(
    () => new Set(profiles.filter((p) => p.subscribed).map((p) => p.id)),
  );
  const save = useSaveSubscriptions();
  if (profiles.length === 0) {
    return (
      <SectionCard title="Weekly digest subscriptions">
        <Text c="dimmed" size="sm">
          No profiles to subscribe to yet —{" "}
          <Anchor component={Link} to="/profiles/new">
            create one
          </Anchor>
          .
        </Text>
      </SectionCard>
    );
  }
  return (
    <SectionCard title="Weekly digest subscriptions">
      <Text c="dimmed" size="sm" mb="sm">
        Check the profiles whose Monday-morning digest you want emailed to you.
      </Text>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate([...checked], {
            // Reconcile from the server's authoritative response so any profiles
            // that were deactivated between load and save are immediately reflected
            // (rather than leaving the checkbox state diverged from server reality).
            onSuccess: (res) => setChecked(new Set(res.subscribed)),
          });
        }}
      >
        <Stack gap="sm">
          <Stack gap="xs">
            {profiles.map((p) => (
              <Checkbox
                key={p.id}
                label={p.name}
                checked={checked.has(p.id)}
                onChange={(e) =>
                  setChecked((prev) => {
                    const nextSet = new Set(prev);
                    if (e.currentTarget.checked) nextSet.add(p.id);
                    else nextSet.delete(p.id);
                    return nextSet;
                  })
                }
              />
            ))}
          </Stack>
          <Group gap="sm" align="center">
            <Button type="submit" disabled={save.isPending}>
              Save subscriptions
            </Button>
            {save.isSuccess && (
              <Text role="status" c="teal" size="sm">
                Saved.
              </Text>
            )}
          </Group>
        </Stack>
      </form>
    </SectionCard>
  );
}
