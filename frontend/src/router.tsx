import { createBrowserRouter } from "react-router-dom";

import { CockpitChat } from "./cockpit/chat/CockpitChat";
import { CockpitSidebar } from "./cockpit/CockpitSidebar";
import { CallDetail } from "./routes/CallDetail";
import { CallsFilters } from "./routes/CallsFilters";
import { Cockpit } from "./routes/Cockpit";
import { Dashboard } from "./routes/Dashboard";
import { WorkspaceCreate } from "./routes/WorkspaceCreate";
import { WorkspaceList } from "./routes/WorkspaceList";
import { WorkspacesFilters } from "./routes/WorkspacesFilters";
import { AppShell } from "./shell/AppShell";

export const router = createBrowserRouter([
  {
    // Persistent IDE shell wraps every SPA route via <Outlet/>. This pathless
    // layout route keeps the shell mounted across navigations. Child page paths
    // are relative to "/" and MUST stay in sync with routes.json `spa_routes`
    // (the router.test drift guard enforces it).
    element: <AppShell />,
    children: [
      { index: true, element: <Dashboard />, handle: { Sidebar: CallsFilters } },
      { path: "cockpit", element: <WorkspaceList />, handle: { Sidebar: WorkspacesFilters } },
      { path: "cockpit/new", element: <WorkspaceCreate /> },
      { path: "cockpit/:id", element: <Cockpit />, handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat } },
      { path: "calls/:id", element: <CallDetail /> },
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
