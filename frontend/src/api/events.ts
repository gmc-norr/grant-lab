import { useEffect, useRef } from "react";

type Handlers = Record<string, (data: unknown) => void>;
type Opts = {
  onExpired?: () => void; // 401: session gone -> caller routes to /login
  onNoAccess?: () => void; // 403 AccountInactive -> no-access state
  probeAfter?: number; // N consecutive onerror before probing (default 3)
  resetCursorOnUrlChange?: boolean; // chat: each url is a distinct job stream -> drop the cursor
};

export function useEventStream(url: string, handlers: Handlers, opts?: Opts) {
  const lastId = useRef<string | null>(null); // fresh-connect cursor + reconnect catch-up
  const errors = useRef(0);
  const probing = useRef(false); // single-flight guard
  const backoff = useRef(1000); // capped exponential backoff
  // Keep a ref to the latest handlers so the EventSource callback always reads
  // the current closure values without reopening the stream on every render.
  // The stream lifecycle is still keyed only to `url`.
  const handlersRef = useRef<Handlers>(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    // Guard: empty url means "no active stream" (e.g. no running job yet).
    // Keep the hook call unconditional (Rules of Hooks) but bail here before
    // opening any EventSource — new URL("", origin) would point at the SPA root.
    if (!url) return;

    let es: EventSource | null = null;
    // Opt-in: the chat streams a distinct job per url, so a url change must NOT
    // carry the prior job's cursor. The /api/events feed does NOT opt in — it
    // intentionally resumes across remounts/url tweaks.
    if (opts?.resetCursorOnUrlChange) lastId.current = null;

    const open = () => {
      // Pass the cursor on EVERY (re)open so the server resumes from where we left
      // off even on a brand-new EventSource (reload / remount) where Last-Event-ID
      // is absent.
      const u = new URL(url, window.location.origin);
      if (lastId.current) u.searchParams.set("since", lastId.current);
      es = new EventSource(u.toString(), { withCredentials: true });

      for (const type of Object.keys(handlers)) {
        es.addEventListener(type, (e) => {
          // named events don't reach onmessage; dispatch through ref so the
          // current handler closure is always used, not the one from open time.
          const ev = e as MessageEvent;
          if (ev.lastEventId) lastId.current = ev.lastEventId;
          try {
            handlersRef.current[type]?.(JSON.parse(ev.data));
          } catch {
            /* parse error: skip, keep the stream alive */
          }
        });
      }

      es.onopen = () => {
        errors.current = 0;
        backoff.current = 1000;
      };
      es.onerror = async () => {
        errors.current += 1;
        if (errors.current < (opts?.probeAfter ?? 3) || probing.current) return;
        // EventSource hides the HTTP status; probe /api/me to classify the failure.
        // Single-flight + capped exponential backoff (each probe opens a DB session).
        probing.current = true;
        try {
          const r = await fetch("/api/me", { credentials: "same-origin" });
          if (r.status === 401) {
            es?.close();
            opts?.onExpired?.();
            return;
          }
          if (r.status === 403) {
            es?.close();
            opts?.onNoAccess?.();
            return;
          }
          // 200 (or transient): let EventSource keep retrying; just pace future probes.
          backoff.current = Math.min(backoff.current * 2, 30_000);
        } finally {
          setTimeout(() => {
            probing.current = false;
          }, backoff.current);
        }
      };
    };

    open();
    return () => {
      es?.close();
    };
  }, [url]);
}
