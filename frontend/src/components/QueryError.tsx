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
