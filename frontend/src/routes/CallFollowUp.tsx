import { Alert, Button, Group, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import type { CallDetail } from "../api/calls";
import { callCalendar, callShareUrl } from "./callCalendar";

export function CallFollowUp({ call, profileId }: { call: CallDetail["call"]; profileId: number | null }) {
  const [copied, setCopied] = useState(false);
  const [fallback, setFallback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function copy() {
    const url = callShareUrl(window.location.origin, call.id, profileId);
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setFallback(null);
    } catch {
      setFallback(url);
      setCopied(false);
    }
  }
  function download() {
    try {
      const content = callCalendar(call);
      if (!content) return;
      const anchor = document.createElement("a");
      anchor.href = `data:text/calendar;charset=utf-8,${encodeURIComponent(content)}`;
      anchor.download = `call-${call.id}-deadline.ics`;
      anchor.click();
      setError(null);
    } catch {
      setError("Couldn't create the calendar entry.");
    }
  }
  return <>
    <Group gap="xs">
      <Button variant="subtle" size="sm" leftSection={<span className="codicon codicon-link" aria-hidden="true" />} onClick={() => void copy()}>Copy link</Button>
      <Button variant="subtle" size="sm" leftSection={<span className="codicon codicon-calendar" aria-hidden="true" />} disabled={!call.deadline} title={call.deadline ? "Download application deadline" : "No deadline published"} onClick={download}>Add to calendar</Button>
      {copied && <Text role="status" size="sm">Link copied</Text>}
    </Group>
    {fallback && <TextInput label="Call link" value={fallback} readOnly onFocus={event => event.currentTarget.select()} />}
    {error && <Alert role="alert" color="red">{error}</Alert>}
  </>;
}