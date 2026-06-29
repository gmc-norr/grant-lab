import { Anchor, Box, Group, Stack, Text } from "@mantine/core";
import { Link } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";

// One call in the list: title link (to the React detail route), a muted
// meta line (source · deadline), the banded match pill, and a "Start drafting"
// action that opens the React from-call create form. profileId is the dashboard's
// resolved effective profile (carried so Phase-0 can snapshot it).
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const params = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) params.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${params.toString()}`;
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Box miw={0}>
        <Anchor
          component={Link}
          fw={500}
          to={profileId != null ? `/calls/${call.id}?profile=${profileId}` : `/calls/${call.id}`}
        >
          {call.title}
        </Anchor>
        <Text mt="0.15rem" c="dimmed" fz="0.85rem">
          {call.source_name && <Text span>{call.source_name}</Text>}
          {call.source_name && call.deadline && <Text span aria-hidden> · </Text>}
          {call.deadline && <Deadline iso={call.deadline} />}
        </Text>
      </Box>
      <Stack gap="0.3rem" align="flex-end" flex="0 0 auto">
        <MatchBadge score={call.aggregate_score} />
        <Anchor component={Link} fz="0.8rem" fw={500} to={startHref} style={{ whiteSpace: "nowrap" }}>
          Start drafting
        </Anchor>
      </Stack>
    </Group>
  );
}
