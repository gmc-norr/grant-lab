import { Group, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { dashboardParamsFrom, useDashboard } from "../api/dashboard";

// The dashboard header strip: breadcrumb · the 3 summary counts (shown verbatim
// from the read-model — `high_score` is the server's >=70 top-match count, not
// recomputed from MatchBadge bands). Reads the shared useDashboard
// query (deduped with the list + filters). Sign-out lives in the shell TitleBar.
export function DashboardHeader() {
  const [sp] = useSearchParams();
  const { data } = useDashboard(dashboardParamsFrom(sp));
  const s = data?.summary;

  return (
    <Group
      component="header"
      gap="md"
      align="center"
      px="0.25rem"
      py="xs"
      style={{ borderBottom: "1px solid var(--app-border)" }}
    >
      <Text fw={600}>Calls</Text>
      <Group gap="md" c="dimmed" fz="0.85rem">
        <Text span><Text span fw={700}>{s?.open_count ?? 0}</Text> open</Text>
        <Text span><Text span fw={700}>{s?.high_score ?? 0}</Text> top matches</Text>
        <Text span><Text span fw={700}>{s?.soon_deadline ?? 0}</Text> closing within 30 days</Text>
      </Group>
    </Group>
  );
}
