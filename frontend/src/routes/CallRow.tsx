// frontend/src/routes/CallRow.tsx
import { Anchor, Box, Group, Stack, Text } from "@mantine/core";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import { selectionSearch } from "./callSelection";

// One call in the list: the title selects the call into the right reading pane
// (URL ?selected=id, preserving filters), a muted meta line (source · deadline),
// and the banded match pill. The whole row is click-to-select for mouse users;
// the title anchor remains the keyboard/interactive target, so the row onClick
// ignores clicks that land on an <a>/<button> (no nested-interactive a11y trap,
// no double navigation). profileId is the dashboard's resolved effective
// profile, pinned so the pane ranks the same.
//
// There is NO "Start drafting" action: it linked to /cockpit/new, which release 1
// does not mount. Pinned by noCockpitLinks.test.tsx and by CallRow.test.tsx.
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const selected = sp.get("selected") === String(call.id);
  const onRowClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a,button")) return; // links/buttons handle themselves
    navigate({ search: selectionSearch(sp, call.id, profileId) });
  };
  return (
    <Group
      component="li"
      justify="space-between"
      align="flex-start"
      gap="sm"
      wrap="nowrap"
      px="0.25rem"
      py="xs"
      onClick={onRowClick}
      aria-current={selected ? "true" : undefined}
      bg={selected ? "var(--app-selected)" : undefined}
      style={{ borderBottom: "1px solid var(--app-border)", cursor: "pointer" }}
    >
      <Box miw={0}>
        <Anchor component={Link} fw={500} to={{ search: selectionSearch(sp, call.id, profileId) }}>
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
      </Stack>
    </Group>
  );
}
