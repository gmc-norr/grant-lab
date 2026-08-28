import type { ReactNode } from "react";

import { Group, Text } from "@mantine/core";

/** The one pane header. Same element CallsFilters and WorkspacesFilters already
 *  use, plus an optional count, so every pane in the app is headed identically.
 *
 *  Deliberately NOT collapsible: one view renders at a time, so a toggle would
 *  only let the user hide the thing they just asked for — and a folded group
 *  four down a scrolling pane is the defect this design removes. */
export function PaneHeader({
  title,
  count,
  children,
}: {
  title: string;
  count?: number;
  children?: ReactNode;
}) {
  return (
    <Group justify="space-between" px="sm" pt="sm" pb={4} wrap="nowrap">
      <Text component="h2" tt="uppercase" fz="0.7rem" fw={600} c="dimmed">
        {title}
        {count != null && ` ${count}`}
      </Text>
      {children}
    </Group>
  );
}
