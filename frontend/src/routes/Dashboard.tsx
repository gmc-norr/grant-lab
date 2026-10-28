import { Box, Text } from "@mantine/core";
import { useSearchParams } from "react-router-dom";

import { Pagination } from "../components/Pagination";
import { QueryError } from "../components/QueryError";
import { dashboardParamsFrom, useDashboard } from "../api/dashboard";
import { CallRow } from "./CallRow";
import { DashboardHeader } from "./DashboardHeader";

export function Dashboard() {
  const [sp, setSp] = useSearchParams();
  const params = dashboardParamsFrom(sp);
  const { data, error, isLoading, isError, isFetching, refetch } = useDashboard(params);

  if (isLoading) return <Text p="md">Loading…</Text>;
  // The same dead end the triage feed had, on the full edition's list.
  if (isError || !data)
    return (
      <QueryError
        message="Couldn't load calls."
        error={error}
        onRetry={() => void refetch()}
        retrying={isFetching}
      />
    );

  return (
    <Box component="main" px="md">
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
