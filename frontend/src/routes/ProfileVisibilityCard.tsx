import { Button, Card, Group, SegmentedControl, Stack, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";

import type { ProfileVisibility } from "../api/profiles";
import {
  useAddProfileViewer,
  useProfileViewers,
  useReplaceProfileViewers,
  useSetProfileVisibility,
} from "../api/profiles";

/** "Who can see this" — the owner's sharing control for one profile.
 *
 *  Mounted only when the detail payload says `can_edit`; a reader who cannot
 *  edit has nothing to set here, and the endpoints behind it would 403 anyway.
 *
 *  The two writes are INDEPENDENT, deliberately: `visibility` says who reaches
 *  the profile without being named, and the viewer list says who is named.
 *  Emptying the list is not "make it public" — the server keeps a viewer-less
 *  private profile private, and so must this card.
 */
export function ProfileVisibilityCard({
  profileId,
  visibility,
}: {
  profileId: number;
  visibility: ProfileVisibility;
}) {
  const setVisibility = useSetProfileVisibility(profileId);
  // The prop is the server's last word; the mutation's in-flight variable and
  // its answer are newer. No local state to fall out of sync with a refetch.
  const current: ProfileVisibility = setVisibility.isPending
    ? (setVisibility.variables ?? visibility)
    : (setVisibility.data?.visibility ?? visibility);
  const isPrivate = current === "private";

  const viewers = useProfileViewers(profileId, isPrivate);
  const rows = viewers.data?.viewers ?? [];
  const addViewer = useAddProfileViewer(profileId);
  const replaceViewers = useReplaceProfileViewers(profileId);
  const [email, setEmail] = useState("");
  const busy = addViewer.isPending || replaceViewers.isPending;

  return (
    <Card withBorder radius="md" p="md" bg="var(--app-sidebar)" mb="md">
      <Title order={2} size="h5" mb="sm">
        Who can see this
      </Title>
      <SegmentedControl
        aria-label="Who can see this profile"
        value={isPrivate ? "private" : "org"}
        onChange={(v) => setVisibility.mutate(v as ProfileVisibility)}
        disabled={setVisibility.isPending}
        data={[
          { label: "Only me and admins", value: "private" },
          { label: "Everyone here", value: "org" },
        ]}
      />
      {setVisibility.isError && (
        <Text size="sm" c="red" role="alert" mt="xs">
          Couldn't change who can see this profile.
        </Text>
      )}

      {isPrivate ? (
        <Stack gap="xs" mt="md">
          <Text size="sm" c="dimmed">
            Shared with these people. Everyone else — admins aside — sees no trace of it.
          </Text>
          {viewers.isError ? (
            <Text size="sm" c="red" role="alert">
              Couldn't load who this is shared with.
            </Text>
          ) : viewers.isPending ? (
            // Without this arm the DEFAULT path of a fresh mount states something
            // false: `rows` is [] until GET /viewers answers, so the card told
            // every owner of a private profile that it was shared with nobody.
            // `isPending` (not `isLoading`) is exact here because this block only
            // renders while the query is enabled — pending means "no answer yet",
            // never "switched off".
            <Text size="sm">Loading who can see this…</Text>
          ) : rows.length === 0 ? (
            <Text size="sm">Not shared with anyone yet.</Text>
          ) : (
            <Stack gap={4} component="ul" style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {rows.map((v) => (
                <Group key={v.user_id} component="li" justify="space-between" wrap="nowrap" gap="sm">
                  <div>
                    {/* Name AND email: a list of ids is unusable, and two
                        colleagues can share a first name. */}
                    <Text size="sm">{v.name ?? v.email}</Text>
                    <Text size="xs" c="dimmed">
                      {v.email}
                    </Text>
                  </div>
                  <Button
                    variant="subtle"
                    size="compact-sm"
                    aria-label={`Remove ${v.email}`}
                    disabled={busy}
                    onClick={() =>
                      replaceViewers.mutate(
                        rows.filter((r) => r.user_id !== v.user_id).map((r) => r.user_id),
                      )
                    }
                  >
                    Remove
                  </Button>
                </Group>
              ))}
            </Stack>
          )}
          <Group gap="xs" align="flex-end" wrap="nowrap">
            <TextInput
              label="Share with (email)"
              placeholder="colleague@umu.se"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.currentTarget.value)}
              style={{ flex: 1 }}
            />
            <Button
              disabled={busy || !email.trim()}
              onClick={() =>
                addViewer.mutate(email.trim(), { onSuccess: () => setEmail("") })
              }
            >
              Add
            </Button>
          </Group>
          {addViewer.isError && (
            // ONE generic message for every refusal. The server deliberately
            // answers the same 400 whether the address is unknown, inactive or
            // simply not addable, so that this form is not a way to find out who
            // has an account — saying "no such user" here would undo that.
            <Text size="sm" c="red" role="alert">
              Couldn't add that address.
            </Text>
          )}
          {replaceViewers.isError && (
            <Text size="sm" c="red" role="alert">
              Couldn't update the list.
            </Text>
          )}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed" mt="md">
          Everyone with an account here can see this profile and rank calls against it.
        </Text>
      )}
    </Card>
  );
}
