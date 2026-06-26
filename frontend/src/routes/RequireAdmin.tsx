import type { ReactNode } from "react";

import { useSession } from "../api/session";

// Client-side admin gate. The /api endpoints 403 on their own (the backstop);
// this avoids a broken-page flash on direct navigation. Unauthenticated users
// never reach here — useSession's 401 redirects to /login via unwrap.
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading, isError } = useSession();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load your session.</p>;
  if (!data.capabilities.admin) {
    return (
      <main role="alert">
        <h1>Not authorized</h1>
        <p>You need admin access to view this page.</p>
      </main>
    );
  }
  return <>{children}</>;
}
