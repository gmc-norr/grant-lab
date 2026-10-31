import { Alert, Anchor, Badge, Box, Button, Card, Code, Group, Table, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";

import {
  useAdminDashboard,
  useFetchSource,
  useReenableDigestUser,
  useRerank,
  useSendDigest,
} from "../api/adminDashboard";

// Ported verbatim from admin.html _ok_msgs / _err_msgs (admin.html:23-31).
const OK_FETCH = "Fetch triggered — the source will refresh shortly.";
const OK_DIGEST = "Weekly digest triggered — it will send shortly.";
const OK_REENABLE = "User re-enabled for the weekly digest.";
const OK_RERANK = "Re-ranking enqueued for all active profiles.";
const ERR_FETCH = "Couldn't trigger the fetch — the scheduler or this source's job is unavailable.";
const ERR_DIGEST = "Digest isn't configured (no email backend), so it can't be sent.";

type Banner = { kind: "ok" | "warn"; text: string } | null;

const fmtDt = (s: string | null) => (s ? new Date(s).toLocaleString() : null);

// The source's state in one word, from the backend's verdict (web/api/admin.py
// source_status). With email alerts unconfigured, this is the only place a
// dead source shows itself (beta review 2026-10-31 F8).
type SourceRow = { status: "ok" | "failing" | "never_fetched" | "stale"; consecutive_failures: number };
function sourceStatus(s: SourceRow): { label: string; color: string } {
  switch (s.status) {
    case "failing":
      return { label: `Failing · ${s.consecutive_failures} in a row`, color: "red" };
    case "never_fetched":
      return { label: "Never fetched", color: "gray" };
    case "stale":
      return { label: "Stale", color: "yellow" };
    default:
      return { label: "OK", color: "teal" };
  }
}

export function AdminDashboard() {
  const { data, isLoading, isError } = useAdminDashboard();
  const [banner, setBanner] = useState<Banner>(null);
  const rerank = useRerank();
  const sendDigest = useSendDigest();
  const reenable = useReenableDigestUser();
  const fetchSource = useFetchSource();

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data)
    return (
      <Box component="main" role="alert" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
        <Text>Couldn't load the admin dashboard.</Text>
      </Box>
    );

  const d = data;

  const onFetch = (slug: string, name: string) =>
    fetchSource.mutate({ slug }, {
      onSuccess: (r) =>
        setBanner(r.triggered
          ? { kind: "ok", text: `${OK_FETCH} (${name})` }
          : { kind: "warn", text: `${ERR_FETCH} (${name})` }),
    });

  const onRerank = () => {
    if (!window.confirm(
      `This enqueues up to ${d.rerank_estimate} ranking jobs and may take a while. Continue?`
    )) return;
    rerank.mutate(undefined, { onSuccess: () => setBanner({ kind: "ok", text: OK_RERANK }) });
  };

  const onSendDigest = () =>
    sendDigest.mutate(undefined, {
      onSuccess: (r) =>
        setBanner(r.triggered ? { kind: "ok", text: OK_DIGEST } : { kind: "warn", text: ERR_DIGEST }),
    });

  const onReenable = (userId: number) =>
    reenable.mutate({ user_id: userId },
      { onSuccess: () => setBanner({ kind: "ok", text: OK_REENABLE }) });

  return (
    <Box component="main" maw="60rem" mx="auto" px="xs" pt="md" pb="xl">
      <Title order={1}>Operational health</Title>
      {banner && (
        <Alert
          role={banner.kind === "ok" ? "status" : "alert"}
          color={banner.kind === "ok" ? "teal" : "red"}
          mb="md"
        >
          {banner.text}
        </Alert>
      )}

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Source health</Title>
        <Table horizontalSpacing="sm" verticalSpacing="xs" fz="sm">
          <Table.Thead><Table.Tr><Table.Th>Source</Table.Th><Table.Th>Status</Table.Th><Table.Th>Last fetched</Table.Th><Table.Th>Calls</Table.Th><Table.Th>Actions</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>
            {d.sources.map((s) => (
              <Table.Tr key={s.slug}>
                <Table.Td>{s.name} <Code>{s.slug}</Code></Table.Td>
                <Table.Td>
                  <Badge variant="light" color={sourceStatus(s).color} data-source-status={s.slug}>
                    {sourceStatus(s).label}
                  </Badge>
                </Table.Td>
                <Table.Td>{fmtDt(s.last_fetched_at) ?? <Text span c="dimmed">never</Text>}</Table.Td>
                <Table.Td><Text span fw={700}>{s.call_count}</Text></Table.Td>
                <Table.Td>
                  <Button size="xs" variant="default" disabled={fetchSource.isPending}
                          onClick={() => onFetch(s.slug, s.name)}>
                    Fetch now
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Ranking queue</Title>
        <Group gap="lg" mb="sm">
          <Box><Text c="dimmed" size="xs">Pending</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.pending}</Text></Box>
          <Box><Text c="dimmed" size="xs">Running</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.running}</Text></Box>
          <Box><Text c="dimmed" size="xs">Done</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.done}</Text></Box>
          <Box><Text c="dimmed" size="xs">Failed attempts</Text><Text fz="1.4rem" fw={700}>{d.queue_counts.failed}</Text></Box>
        </Group>
        {/* A count of JOB ROWS, so it never goes down: a pair that failed
            twice and then scored is two failures here. What is still unscored
            right now is on the calls page, per profile. */}
        <Text c="dimmed" size="xs" mb="sm" data-field="failed-attempts-hint">
          Failed attempts are all-time and include ones that later succeeded. Calls still
          without a score are counted on the calls page.
        </Text>
        <Button size="xs" variant="default" disabled={rerank.isPending} onClick={onRerank}>
          Re-rank everything (open calls × active profiles)
        </Button>
        <Text c="dimmed" size="sm" mt="xs">
          Enqueues {d.rerank_estimate} ranking jobs ({d.active_profiles_count} active profiles
          × {d.open_calls_count} open calls).
        </Text>
      </Card>

      {d.calibration_warning && (
        <Alert role="alert" color="yellow" mb="md">
          <Text span fw={700}>Embedding thresholds uncalibrated.</Text> {d.calibration_warning}
        </Alert>
      )}

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Embedding coverage</Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Calls</Text>
          <Text component="dd" size="sm" m={0}><Text span fw={700}>{d.embedding_coverage.call_embedded}/{d.embedding_coverage.call_total} embedded</Text>{" "}
            <Text span c="dimmed">({d.embedding_coverage.call_stale} stale, model <Code>{d.embedding_coverage.current_model}</Code>)</Text></Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Profiles</Text>
          <Text component="dd" size="sm" m={0}><Text span fw={700}>{d.embedding_coverage.profile_embedded}/{d.embedding_coverage.profile_total} embedded</Text>{" "}
            <Text span c="dimmed">({d.embedding_coverage.profile_stale} stale, model <Code>{d.embedding_coverage.current_model}</Code>)</Text></Text>
        </Box>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Digest (weekly)</Title>
        <Box component="dl" m={0}>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Last digest sent</Text>
          <Text component="dd" size="sm" m={0}>{fmtDt(d.digest.last_sent_at) ?? <Text span c="dimmed">No digests sent</Text>}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Runs this week</Text><Text component="dd" size="sm" m={0}>{d.digest.runs_this_week}</Text>
          <Text component="dt" size="xs" c="dimmed" mt="xs">Disabled users (3+ bounces)</Text><Text component="dd" size="sm" m={0}>{d.digest.disabled_users}</Text>
        </Box>
        {d.digest.disabled_user_rows.length > 0 ? (
          <Box component="ul" p={0}>
            {d.digest.disabled_user_rows.map((u) => (
              <Group component="li" key={u.id} gap="xs" align="center" mt="xs">
                {u.email} <Text span c="dimmed">disabled {fmtDt(u.disabled_at)}</Text>
                <Button size="xs" variant="default" disabled={reenable.isPending}
                        onClick={() => onReenable(u.id)}>Re-enable</Button>
              </Group>
            ))}
          </Box>
        ) : <Text c="dimmed" size="sm">No disabled users.</Text>}
        <Button size="xs" variant="default" mt="sm" disabled={sendDigest.isPending} onClick={onSendDigest}>
          Send digest now
        </Button>
      </Card>

      <Card component="section" withBorder radius="md" p="md" my="md">
        <Title order={2} size="h5" mb="sm">Ranking quality — Kendall's τ (last run)</Title>
        {/* UNGATED since 2026-09-07: router.simple.tsx mounts /admin/golden-set in
            the match edition too, so the `!isMatchEdition` guard that used to wrap
            this link would now hide a page release 1 can actually reach — and this
            card's tau number is the reason someone goes there. AdminDashboard
            .test.tsx flips the edition and asserts the link is present either way,
            which is what reddens if a gate comes back. */}
        <Text mb="sm"><Anchor component={Link} to="/admin/golden-set">Manage golden set →</Anchor></Text>
        {d.golden_set_report ? (
          <Box component="dl" m={0}>
            <Text component="dt" size="xs" c="dimmed" mt="xs">Curated triples</Text><Text component="dd" size="sm" m={0}>{d.golden_set_report.n_fixture}</Text>
            <Text component="dt" size="xs" c="dimmed" mt="xs">τ (n={d.golden_set_report.n_compared})</Text>
            <Text component="dd" size="sm" m={0}>{d.golden_set_report.tau !== null ? <Text span fw={700}>{d.golden_set_report.tau.toFixed(3)}</Text> : "—"}</Text>
          </Box>
        ) : (
          <Text c="dimmed" size="sm">Golden-set evaluation hasn't run yet — happens Mondays at 06:00 Europe/Stockholm.</Text>
        )}
      </Card>
    </Box>
  );
}
