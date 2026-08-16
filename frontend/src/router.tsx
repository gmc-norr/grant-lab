import { lazy, Suspense } from "react";
import { Center, Group, Loader, Text, Title } from "@mantine/core";
import { createBrowserRouter } from "react-router-dom";

import { CockpitChat } from "./cockpit/chat/CockpitChat";
import { CockpitSidebar } from "./cockpit/CockpitSidebar";
import { AccountNoAccess } from "./routes/AccountNoAccess";
import { AccountPending } from "./routes/AccountPending";
import { AuthError } from "./routes/AuthError";
import { CallDetail } from "./routes/CallDetail";
import { CallReadingPane } from "./routes/CallReadingPane";
import { CallsFilters } from "./routes/CallsFilters";
import { Login } from "./routes/Login";
import { PreAuthLayout } from "./routes/PreAuthLayout";
import { ProfileDetail } from "./routes/ProfileDetail";
import { ProfileEdit } from "./routes/ProfileEdit";
import { ProfileNew } from "./routes/ProfileNew";
import { ProfilesList } from "./routes/ProfilesList";
import { Register } from "./routes/Register";
import { Settings } from "./routes/Settings";
import { Dashboard } from "./routes/Dashboard";
import { WorkspaceCreate } from "./routes/WorkspaceCreate";
import { WorkspaceList } from "./routes/WorkspaceList";
import { WorkspacesFilters } from "./routes/WorkspacesFilters";
import { AppShell } from "./shell/AppShell";

// Heavy routes are code-split so dashboard-only users don't download the
// CodeMirror editor bundle or the admin stack on initial load.
// NOTE: keep `key={id}` on the Cockpit element — it forces a full remount when
// the workspace id changes (a prior batch added this).
const Cockpit = lazy(() => import("./routes/Cockpit").then((m) => ({ default: m.Cockpit })));
const AdminLayout = lazy(() => import("./routes/AdminLayout").then((m) => ({ default: m.AdminLayout })));
const AdminDashboard = lazy(() => import("./routes/AdminDashboard").then((m) => ({ default: m.AdminDashboard })));
const AdminDeletedWorkspaces = lazy(() =>
  import("./routes/AdminDeletedWorkspaces").then((m) => ({ default: m.AdminDeletedWorkspaces })),
);
const AdminDeletedWorkspaceDetail = lazy(() =>
  import("./routes/AdminDeletedWorkspaceDetail").then((m) => ({ default: m.AdminDeletedWorkspaceDetail })),
);
const AdminDrafting = lazy(() => import("./routes/AdminDrafting").then((m) => ({ default: m.AdminDrafting })));
const AdminGoldenSet = lazy(() => import("./routes/AdminGoldenSet").then((m) => ({ default: m.AdminGoldenSet })));
const AdminPendingUsers = lazy(() => import("./routes/AdminPendingUsers").then((m) => ({ default: m.AdminPendingUsers })));
const AdminUsers = lazy(() => import("./routes/AdminUsers").then((m) => ({ default: m.AdminUsers })));

function RouteSpinner() {
  return (
    <Center component="main" h="100%" p="xl">
      <Group gap="sm">
        <Loader size="sm" />
        <Text c="dimmed">Loading…</Text>
      </Group>
    </Center>
  );
}

export const router = createBrowserRouter([
  {
    // Persistent IDE shell wraps every SPA route via <Outlet/>. This pathless
    // layout route keeps the shell mounted across navigations. Child page paths
    // are relative to "/" and MUST stay in sync with routes.json `spa_routes`
    // (the router.test drift guard enforces it).
    element: <AppShell />,
    children: [
      { index: true, element: <Dashboard />, handle: { Sidebar: CallsFilters, RightPane: CallReadingPane, rightPaneLabel: "reading pane" } },
      { path: "cockpit", element: <WorkspaceList />, handle: { Sidebar: WorkspacesFilters } },
      { path: "cockpit/new", element: <WorkspaceCreate /> },
      {
        path: "cockpit/:id",
        element: (
          <Suspense fallback={<RouteSpinner />}>
            <Cockpit />
          </Suspense>
        ),
        handle: { Sidebar: CockpitSidebar, RightPane: CockpitChat },
      },
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
            path: "drafting",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDrafting />
              </Suspense>
            ),
          },
          {
            path: "golden-set",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminGoldenSet />
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
          {
            path: "deleted-workspaces",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDeletedWorkspaces />
              </Suspense>
            ),
          },
          {
            path: "deleted-workspaces/:workspaceId",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminDeletedWorkspaceDetail />
              </Suspense>
            ),
          },
        ],
      },
      // In-app soft 404 ONLY (client-side reached). NOT the route authority —
      // the server returns index.html only for manifest SPA routes, so an
      // unknown typed URL hits FastAPI's real 404, never this. Must not turn
      // unknown paths into 200s.
      {
        path: "*",
        element: (
          <Center component="main" role="alert" p="xl">
            <Title order={1}>Not found</Title>
          </Center>
        ),
      },
    ],
  },
  {
    // Pre-auth pages: a SECOND pathless layout, sibling to AppShell. No IDE shell,
    // no useSession (the anti-401-loop invariant). An exact path like "login"
    // out-ranks AppShell's "*" splat, so /login matches HERE, not the soft-404.
    element: <PreAuthLayout />,
    children: [
      { path: "login", element: <Login /> },
      { path: "register", element: <Register /> },
      { path: "auth/pending", element: <AccountPending /> },
      { path: "auth/no-access", element: <AccountNoAccess /> },
      { path: "auth/error", element: <AuthError /> },
    ],
  },
]);
