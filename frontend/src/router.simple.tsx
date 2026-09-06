import { lazy, Suspense } from "react";
import { Center, Group, Loader, Text, Title } from "@mantine/core";
import { createBrowserRouter } from "react-router-dom";

import { AccountNoAccess } from "./routes/AccountNoAccess";
import { AccountPending } from "./routes/AccountPending";
import { AuthError } from "./routes/AuthError";
import { BoardLayout } from "./routes/BoardLayout";
import { BoardScreen } from "./routes/BoardScreen";
import { CallDetail } from "./routes/CallDetail";
import { Login } from "./routes/Login";
import { PreAuthLayout } from "./routes/PreAuthLayout";
import { ProfileDetail } from "./routes/ProfileDetail";
import { ProfileEdit } from "./routes/ProfileEdit";
import { ProfileNew } from "./routes/ProfileNew";
import { ProfilesList } from "./routes/ProfilesList";
import { Register } from "./routes/Register";
import { Settings } from "./routes/Settings";
import { TriageFeed } from "./routes/TriageFeed";
import { SimpleShell } from "./shell/SimpleShell";

// Call Match release 1. This file's import list IS the exclusion mechanism:
// nothing here reaches a cockpit, workspace, editor or collab module, lazily or
// otherwise, so Vite's tree-shaking has no path to that code and never emits it.
//
// Do NOT lean on lazy() for exclusion. router.tsx is only partly lazy —
// CockpitChat and CockpitSidebar are static imports at router.tsx:5-6, as are
// WorkspaceList, WorkspaceCreate and WorkspacesFilters — and in any case a
// lazy() import still emits a chunk. Absence from the import graph is what makes
// the code absent from the build.
//
// Route paths MUST stay inside the manifest's owned set (routes.gen.ts) or a
// hard refresh 404s. Pinned by router.simple.test.tsx.
const AdminLayout = lazy(() =>
  import("./routes/AdminLayout").then((m) => ({ default: m.AdminLayout })),
);
const AdminDashboard = lazy(() =>
  import("./routes/AdminDashboard").then((m) => ({ default: m.AdminDashboard })),
);
const AdminPendingUsers = lazy(() =>
  import("./routes/AdminPendingUsers").then((m) => ({ default: m.AdminPendingUsers })),
);
const AdminUsers = lazy(() =>
  import("./routes/AdminUsers").then((m) => ({ default: m.AdminUsers })),
);

function RouteSpinner() {
  return (
    <Center component="div" h="100%" p="xl">
      <Group gap="sm">
        <Loader size="sm" />
        <Text c="dimmed">Loading…</Text>
      </Group>
    </Center>
  );
}

export const simpleRouter = createBrowserRouter([
  {
    element: <SimpleShell />,
    children: [
      // THE EDITION SPLIT, and it is this line rather than a branch inside a
      // shared component. Dashboard/CallRow/MatchBadge/DashboardHeader/
      // Pagination are the FULL edition's list; dropping the import here is what
      // removes them from the match bundle (check_match_bundle.mjs measures it).
      // None of them may be deleted — router.tsx still mounts them.
      { index: true, element: <TriageFeed /> },
      { path: "calls/:id", element: <CallDetail /> },
      { path: "me", element: <Settings /> },
      { path: "profiles", element: <ProfilesList /> },
      { path: "profiles/new", element: <ProfileNew /> },
      { path: "profiles/:id", element: <ProfileDetail /> },
      { path: "profiles/:id/edit", element: <ProfileEdit /> },
      {
        path: "admin",
        element: (
          <Suspense fallback={<RouteSpinner />}>
            <AdminLayout />
          </Suspense>
        ),
        children: [
          {
            index: true,
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDashboard />
              </Suspense>
            ),
          },
          {
            path: "users",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminUsers />
              </Suspense>
            ),
          },
          {
            path: "users/pending",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminPendingUsers />
              </Suspense>
            ),
          },
        ],
      },
      // In-app soft 404 only. The server returns index.html for manifest routes
      // alone, so a typed unknown URL hits FastAPI's real 404, never this.
      {
        path: "*",
        // component="main", matching router.tsx:155. SimpleShell used to supply
        // the landmark; it no longer does (every other release-1 route renders
        // its own), so without this the soft-404 page would have none at all.
        element: (
          <Center component="main" role="alert" p="xl">
            <Title order={1}>Not found</Title>
          </Center>
        ),
      },
    ],
  },
  {
    // Pre-auth pages: a second pathless layout, sibling to SimpleShell. No
    // useSession (the anti-401-loop invariant). An exact path like "login"
    // out-ranks the splat above, so /login matches HERE.
    element: <PreAuthLayout />,
    children: [
      { path: "login", element: <Login /> },
      { path: "register", element: <Register /> },
      { path: "auth/pending", element: <AccountPending /> },
      { path: "auth/no-access", element: <AccountNoAccess /> },
      { path: "auth/error", element: <AuthError /> },
    ],
  },
  {
    // A THIRD pathless layout, sibling to SimpleShell and PreAuthLayout. A
    // board is a match-edition feature (§4.3), so this edition mounts it too
    // — see router.tsx for the same route and BoardLayout.tsx for why it does
    // not use PreAuthLayout despite §6 asking for that.
    element: <BoardLayout />,
    children: [{ path: "b/:slug", element: <BoardScreen /> }],
  },
]);

// The `virtual:app-router` alias imports `router`; router.tsx already exports
// that name. `simpleRouter` stays exported for the direct import in
// router.simple.test.tsx, which must name the file it is pinning.
export { simpleRouter as router };
