import { createBrowserRouter } from "react-router-dom";

import { Dashboard } from "./routes/Dashboard";

export const router = createBrowserRouter([
  // SPA-owned page route. v1 mounts at the temporary `/app` (Plan 2 manifest);
  // Task 9 flips this to `/`. Keep this table in sync with `routes.json` — the
  // SERVER (manifest) is authoritative for what gets index.html.
  { path: "/app", element: <Dashboard /> },
  // In-app catch-all ONLY: a soft 404 for paths the SPA reached client-side via
  // React Router navigation. It is NOT the route authority — the server returns
  // index.html only for manifest SPA routes, so a typed/unknown URL never loads
  // the SPA (it hits FastAPI's real 404). This MUST NOT turn unknown paths into 200s.
  {
    path: "*",
    element: (
      <main role="alert">
        <h1>Not found</h1>
      </main>
    ),
  },
]);
