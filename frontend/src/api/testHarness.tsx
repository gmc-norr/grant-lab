import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";

/** A wrapper bound to a caller-owned QueryClient, so a test can seed keys and
 *  then inspect their invalidation state after a mutation. */
export function harnessFor(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}
