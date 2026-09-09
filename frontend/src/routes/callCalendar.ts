import { createEvent } from "ics";
import type { CallDetail } from "../api/calls";

export function callCalendar(call: CallDetail["call"]): string | null {
  if (!call.deadline) return null;
  const deadline = new Date(call.deadline);
  if (!Number.isFinite(deadline.getTime())) return null;
  const { error, value } = createEvent({
    uid: `grant-lab-call-${call.id}`,
    productId: "grant-lab/call-match",
    title: `${call.title} - application deadline`,
    start: deadline.getTime(),
    startInputType: "utc",
    startOutputType: "utc",
    duration: { seconds: 1 },
    description: `${call.source_name ?? "Funding call"}\n${call.url}`,
    url: call.url,
  });
  if (error) throw error;
  return value ?? null;
}

export function callShareUrl(origin: string, callId: number, profileId: number | null): string {
  const url = new URL(`/calls/${callId}`, origin);
  if (profileId !== null) url.searchParams.set("profile", String(profileId));
  return url.toString();
}