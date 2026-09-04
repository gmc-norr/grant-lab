// frontend/src/routes/CallRow.tsx
import { Anchor, Box, Group, Stack, Text } from "@mantine/core";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { isMatchEdition } from "../edition";
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
// "Start drafting" is GATED on the edition, not deleted. Spec §4.1 said to remove
// it, and removing it made /cockpit/new unreachable in the FULL edition too:
// WorkspaceCreate.tsx:21-30 is from-call-only, so a full build could no longer
// create a drafting workspace through the UI at all, while WorkspaceList.tsx:75,81
// went on telling users to "pick a call to start drafting". That is run-record
// ESCALATE E1, resolved by the human on 2026-09-04. The spec's stated reason for
// deletion — "or release 1 ships two buttons that 404" — is satisfied by gating:
// release 1 does not mount /cockpit/new and does not render this link.
//
// Same mechanism as AdminNav.tsx. noCockpitLinks.test.tsx EXEMPTs this file
// because a source scan cannot tell a gated string literal from an ungated one,
// so BOTH branches are pinned by CallRow.test.tsx instead — those tests are the
// only thing holding this gating.
//
// DESTINATION IS EDITION-DEPENDENT, and that is a reachability fix, not a style
// choice. ?selected=N only does something when a reading pane is mounted to read
// it, which router.tsx:64 arranges via `handle: { RightPane: CallReadingPane }`
// and AppShell.tsx:31-43 renders. router.simple.tsx declares no handle and
// SimpleShell renders only the Outlet — so in the match edition the selection URL
// changes the query string and NOTHING appears, while /calls/:id, which the
// simple router DOES mount, is linked only from CallReadingPane.tsx:50 (not
// mounted). Match therefore goes straight to the full page, carrying the
// resolved profile the same way CallReadingPane.tsx:50 does so the detail page
// ranks against it. The full edition is untouched. Both branches are pinned by
// CallRow.test.tsx.
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const selected = sp.get("selected") === String(call.id);
  const detailTo = `/calls/${call.id}${profileId != null ? `?profile=${profileId}` : ""}`;
  const startParams = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) startParams.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${startParams.toString()}`;
  const rowTarget = isMatchEdition
    ? detailTo
    : { search: selectionSearch(sp, call.id, profileId) };
  const onRowClick = (e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("a,button")) return; // links/buttons handle themselves
    navigate(rowTarget);
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
        <Anchor component={Link} fw={500} to={rowTarget}>
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
        {!isMatchEdition && (
          <Anchor component={Link} fz="0.8rem" fw={500} to={startHref} style={{ whiteSpace: "nowrap" }}>
            Start drafting
          </Anchor>
        )}
      </Stack>
    </Group>
  );
}
