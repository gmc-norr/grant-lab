import { createBrowserRouter } from "react-router-dom";

import { Dashboard } from "./routes/Dashboard";

export const router = createBrowserRouter([
  // SPA-owned page route. Task 9 flipped this from the temporary `/app` scaffold
  // to `/` (the React dashboard now owns the root; the Jinja calls_index was
  // retired). Keep this in sync with `routes.json` `spa_routes` — the SERVER
  // (manifest) is authoritative for what gets index.html.
  { path: "/", element: <Dashboard /> },
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
