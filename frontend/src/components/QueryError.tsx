import { Alert, Box, Button, Stack, Text } from "@mantine/core";

import { ApiError } from "../api/client";

// THE SHARED QUERY-ERROR SCREEN (review 2026-10-26, A8 "Feed errors are slow and
// dead-end"). The measured state it replaces: "Couldn't load calls." as a bare
// <p>, with no rail, no toolbar, no <main> and no retry — 0 of the 9 error
// screens checked had a Retry button, and most queries are `retry: false` with
// `refetchOnWindowFocus: false`, so the only way out was a browser reload.
export type QueryErrorProps = {
  /** The human sentence for THIS screen ("Couldn't load calls."). Always shown,
   *  so the member is never left reading only a machine code. */
  message: string;
  /** The query's own error, if any. Only an ApiError's own sentence is quoted:
   *  a `TypeError: Failed to fetch` is noise, not information. */
  error?: unknown;
  /** Usually `() => refetch()`. Written as a plain callback rather than taking
   *  the query object so a mutation screen can use this too. */
  onRetry: () => void;
  /** Disables the button while the refetch is in flight, so an impatient click
   *  does not queue a second one. */
  retrying?: boolean;
};

export function QueryError({ message, error, onRetry, retrying = false }: QueryErrorProps) {
  const detail = error instanceof ApiError ? error.userMessage : null;
  return (
    <Box component="main" maw="32rem" mx="auto" px="md" py="xl">
      <Stack gap="sm" align="flex-start">
        {/* The landmark and the live region stay separate elements: role="alert"
            on the <main> would override its implicit landmark role, and the e2e
            walk counts landmarks as its proof a page rendered at all. */}
        <Alert role="alert" color="red" variant="light" w="100%">
          <Text>{message}</Text>
          {detail !== null && (
            <Text size="sm" c="dimmed" mt={4}>
              {detail}
            </Text>
          )}
        </Alert>
        <Button variant="default" onClick={onRetry} disabled={retrying}>
          {retrying ? "Retrying…" : "Retry"}
        </Button>
      </Stack>
    </Box>
  );
}

export type QueryErrorBannerProps = {
  /** The human sentence for THIS list ("Couldn't refresh the list."). */
  message: string;
  /** The query's own error. Same rule as QueryError: only an ApiError speaks. */
  error?: unknown;
  onRetry: () => void;
  retrying?: boolean;
};

// THE REFETCH HALF (review 2026-10-26 final, IMPORTANT 1). A failed REFETCH is
// not a failed load: TanStack keeps the last good `data` and still sets
// `status: "error"`, so the member has a screen full of real cards and a list
// that may be a few seconds behind. The takeover above is the wrong answer to
// that — it throws away good data AND the decision the member just made (the
// hide landed; the queued "why did you hide it?" prompt went with the screen).
//
// NOT `QueryError` rendered inline, for two reasons that are both structural:
// it renders its OWN `<main>` landmark, so inlining it nests a second landmark
// inside the feed's (invalid, and the e2e walk counts landmarks as its proof a
// page rendered at all); and its centred 32 rem column is a layout designed to
// BE the page. This is the same information at the altitude of a strip over the
// list it is about — `role="alert"` so it is announced without stealing focus,
// and the same Retry affordance, because the policy is one retry and ~1 s, so a
// member who wants the list now needs a way to ask.
export function QueryErrorBanner({
  message,
  error,
  onRetry,
  retrying = false,
}: QueryErrorBannerProps) {
  const detail = error instanceof ApiError ? error.userMessage : null;
  return (
    <Alert role="alert" color="yellow" variant="light" mb="md" data-testid="stale-list-banner">
      <Text size="sm">
        {message} {detail !== null && <Text span c="dimmed">({detail})</Text>}
      </Text>
      <Button mt="xs" size="xs" variant="default" onClick={onRetry} disabled={retrying}>
        {retrying ? "Retrying…" : "Retry"}
      </Button>
    </Alert>
  );
}
