// `virtual:app-router` is a build-time alias (vite.config.ts) that resolves to
// router.tsx or router.simple.tsx depending on VITE_APP_EDITION.
declare module "virtual:app-router" {
  import type { createBrowserRouter } from "react-router-dom";
  export const router: ReturnType<typeof createBrowserRouter>;
}
