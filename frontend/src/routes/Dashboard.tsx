import { Box, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import { CallRow } from "./CallRow";
import { DashboardHeader } from "./DashboardHeader";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data, isLoading, isError } = useDashboard(params);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) return <Text role="alert" p="md">Couldn&apos;t load calls.</Text>;

  return (
    <Box component="main">
      <DashboardHeader />

      {data.ranking.pending > 0 && <Text>Scoring {data.ranking.pending} calls…</Text>}
      {data.ranking.failed > 0 && (
        <Text>{data.ranking.failed} calls couldn&apos;t be scored automatically.</Text>
      )}

      <Box component="ul" p={0} my="xs">
        {data.items.map((c) => (
          <CallRow key={c.id} call={c} profileId={data.effective.profile_id} />
        ))}
      </Box>

      <Pagination
        page={data.pagination.page}
        hasPrev={data.pagination.has_prev}
        hasNext={data.pagination.has_next}
        onPage={(p) =>
          setSp((prev) => {
            const next = new URLSearchParams(prev);
            next.set("page", String(p));
            return next;
          })
        }
      />
    </Box>
  );
}
