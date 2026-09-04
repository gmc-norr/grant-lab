// Which product edition this BUILD is. Set at build time via VITE_APP_EDITION.
// This is the RUNTIME reader; vite.config.ts reads the same variable at CONFIG
// time to pick which router file enters the module graph. Two readers by
// design — kept in agreement by the config using loadEnv() rather than
// process.env (process.env misses .env-file values entirely), and pinned by
// src/virtual-router.test.ts. The backend has a matching APP_EDITION setting
// (src/call_manager/config.py) and the two must agree in a deployment — a match
// frontend against a full backend merely hides features, but a full frontend
// against a match backend renders links to endpoints that do not exist.
export type Edition = "match" | "full";

// Exported for the unit test: import.meta.env is not injectable at runtime.
export function resolveEdition(raw: string | undefined): Edition {
  // Exact match only. A typo falls back to `full`, the superset, so nothing
  // 404s — the failure mode of guessing is worse than the failure mode of
  // ignoring.
  return raw === "match" ? "match" : "full";
}

export const APP_EDITION: Edition = resolveEdition(
  import.meta.env.VITE_APP_EDITION as string | undefined,
);

export const isMatchEdition = APP_EDITION === "match";
