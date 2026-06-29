// frontend/src/routes/CallReadingPane.tsx
import { ActionIcon, Anchor, Box, Center, Group, Stack, Text } from "@mantine/core";
import { Link, useSearchParams } from "react-router-dom";

import { useCallDetail } from "../api/calls";
import { CallDetailBody } from "./CallDetailBody";
import { deselectSearch } from "./callSelection";

// Inner data-fetcher. Split out so useCallDetail is called unconditionally (the
// outer pane decides placeholder-vs-preview by conditionally RENDERING this).
function CallPreview({ callId, profile }: { callId: number; profile: number | null }) {
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);
  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }
  return (
    <Stack gap="lg" px="md" py="md">
      <CallDetailBody {...data} />
    </Stack>
  );
}

// Dashboard reading pane (the shell's RightPane on the calls route). Selection is
// the `selected` URL param; `profile` mirrors the list's effective profile so the
// pane's ranking matches the list. Always renders the <aside> landmark.
export function CallReadingPane() {
  const [sp, setSp] = useSearchParams();
  const selected = sp.get("selected");
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;

  return (
    <Box component="aside" aria-label="Call preview" h="100%" style={{ display: "flex", flexDirection: "column" }}>
      {selected ? (
        <>
          <Group
            justify="space-between"
            align="center"
            px="md"
            py="xs"
            flex="0 0 auto"
            style={{ borderBottom: "1px solid var(--app-border)" }}
          >
            <Anchor
              component={Link}
              fz="0.85rem"
              fw={500}
              to={`/calls/${selected}${profile != null ? `?profile=${profile}` : ""}`}
            >
              Open full page ↗
            </Anchor>
            <ActionIcon
              variant="subtle"
              aria-label="Close preview"
              onClick={() => setSp(new URLSearchParams(deselectSearch(sp)))}
            >
              ✕
            </ActionIcon>
          </Group>
          <Box style={{ flex: 1, overflowY: "auto" }}>
            <CallPreview callId={Number(selected)} profile={profile} />
          </Box>
        </>
      ) : (
        <Center flex={1} p="md">
          <Text c="dimmed">Select a call to preview</Text>
        </Center>
      )}
    </Box>
  );
}
