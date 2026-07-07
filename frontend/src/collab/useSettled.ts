import { useEffect, useState } from "react";

import type { CollabProvider } from "./collabProvider";

/**
 * Reactive mirror of `provider.isSettled()` (socket open + drained + idle since the
 * last local edit). The value is time-varying — it flips true a tick after an
 * awareness send drains the WS buffer — so consumers that gate on it (SaveVersionButton,
 * the selection toolbar's Edit/Cite) MUST re-render when it changes, not sample it once
 * at render (the D0 settled-latch). Polls every 200ms, matching SaveVersionButton's
 * original inline poll.
 */
export function useSettled(provider: CollabProvider): boolean {
  const [settled, setSettled] = useState(() => provider.isSettled());
  useEffect(() => {
    setSettled(provider.isSettled()); // re-sample immediately on provider change
    const t = setInterval(() => setSettled(provider.isSettled()), 200);
    return () => clearInterval(t);
  }, [provider]);
  return settled;
}
