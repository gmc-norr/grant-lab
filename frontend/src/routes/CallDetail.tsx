// frontend/src/routes/CallDetail.tsx
import { Anchor, Stack, Text } from "@mantine/core";
import { useEffect } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { useCallDetail } from "../api/calls";
import { useSession } from "../api/session";
import { CallDecisionActions } from "./CallDecisionActions";
import { CallFollowUp } from "./CallFollowUp";
import { MatchCallOverview } from "./MatchCallOverview";

export function CallDetail() {
  const { id = "" } = useParams();
  const callId = Number(id);
  const [sp] = useSearchParams();
  const profileParam = sp.get("profile");
  const profile = profileParam != null && profileParam !== "" ? Number(profileParam) : null;
  const navigate = useNavigate();
  // NO `?profile=` AT ALL (a bookmark, a typed address, a link from outside the
  // feed): use the reader's own default, the same fallback the calls list makes
  // server-side. Without it the page showed no score, no match reasons and
  // greyed-out Save/Hide (UX pass 2026-10-31). Written INTO the URL rather than
  // held in state, so everything below -- the decision keys, the Undo memory,
  // Copy link, the back link -- keeps reading one source. An EMPTY `?profile=`
  // is an explicit "no profile" and is left alone.
  const session = useSession();
  const fallbackProfile = profileParam === null ? (session.data?.user?.default_profile_id ?? null) : null;
  useEffect(() => {
    if (fallbackProfile === null) return;
    const next = new URLSearchParams(sp);
    next.set("profile", String(fallbackProfile));
    navigate({ search: `?${next.toString()}` }, { replace: true });
  }, [fallbackProfile, sp, navigate]);
  // THE URL IS THE STATE (D11). The reader arrived from a triage view on some
  // profile, and `to="/"` would land them in the Inbox of no profile in
  // particular — not where they were. `sp` is what they arrived with, verbatim.
  const backTo = sp.toString() ? `/?${sp.toString()}` : "/";
  const { data, isLoading, isError, error } = useCallDetail(callId, profile);

  if (isLoading) return <Text p="md">Loading…</Text>;
  if (isError || !data) {
    const notFound = (error as { code?: string } | null)?.code === "not_found";
    return <Text role="alert" p="md">{notFound ? "Call not found." : "Couldn't load this call."}</Text>;
  }

  const { call } = data;

  return (
    <Stack component="main" maw="52rem" mx="auto" px="xs" pt="md" pb="xl" gap="lg">
      <Anchor component={Link} to={backTo} size="sm">← All calls</Anchor>

      <MatchCallOverview {...data} actions={<>
        <CallDecisionActions
          key={`${call.id}:${profile}`}
          callId={call.id}
          profileId={profile}
          title={call.title}
          initialState={call.decision_state ?? null}
          initialNote={call.decision_note ?? null}
        />
        <CallFollowUp key={`follow-up:${call.id}:${profile}`} call={call} profileId={profile} />
      </>} />
    </Stack>
  );
}
