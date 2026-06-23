import { createBrowserRouter } from "react-router-dom";

import { Cockpit } from "./routes/Cockpit";
import { Dashboard } from "./routes/Dashboard";
import { AppShell } from "./shell/AppShell";

export const router = createBrowserRouter([
  {
    // Persistent IDE shell wraps every SPA route via <Outlet/>. This pathless
    // layout route keeps the shell mounted across navigations. Child page paths
    // are relative to "/" and MUST stay in sync with routes.json `spa_routes`
    // (the router.test drift guard enforces it).
    element: <AppShell />,
    children: [
      { index: true, element: <Dashboard /> },
      { path: "cockpit/:id", element: <Cockpit /> },
      // In-app soft 404 ONLY (client-side reached). NOT the route authority —
      // the server returns index.html only for manifest SPA routes, so an
      // unknown typed URL hits FastAPI's real 404, never this. Must not turn
      // unknown paths into 200s.
      {
        path: "*",
        element: (
          <main role="alert">
            <h1>Not found</h1>
          </main>
        ),
      },
    ],
  },
]);
