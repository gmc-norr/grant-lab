import { lazy, Suspense } from "react";
import { Center, Group, Loader, Text, Title } from "@mantine/core";
import { createBrowserRouter } from "react-router-dom";

import { RouteError } from "./components/RouteError";
import { AccountNoAccess } from "./routes/AccountNoAccess";
import { AccountPending } from "./routes/AccountPending";
import { AuthError } from "./routes/AuthError";
import { BoardDetail } from "./routes/BoardDetail";
import { BoardLayout } from "./routes/BoardLayout";
import { BoardScreen } from "./routes/BoardScreen";
import { BoardsList } from "./routes/BoardsList";
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

// The SPA's only router. Route paths MUST stay inside the manifest's owned set
// (routes.gen.ts) or a hard refresh 404s; router.test.tsx pins both directions.
const AdminLayout = lazy(() =>
  import("./routes/AdminLayout").then((m) => ({ default: m.AdminLayout })),
);
const AdminDashboard = lazy(() =>
  import("./routes/AdminDashboard").then((m) => ({ default: m.AdminDashboard })),
);
const AdminGoldenSet = lazy(() =>
  import("./routes/AdminGoldenSet").then((m) => ({ default: m.AdminGoldenSet })),
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

export const router = createBrowserRouter([
  {
    // THE ROOT ERROR BOUNDARY for this layout (review 2026-10-26 M-fe-2). The
    // three sibling pathless layouts are the roots there are, so each needs its
    // own: react-router renders the DEEPEST matched route that declares an
    // errorElement, and with none declared anywhere a lazy chunk 404 after a
    // deploy replaced the entire app with react-router's developer screen —
    // "Unexpected Application Error! … 💿 Hey developer 👋", no navigation.
    // App.tsx's ErrorBoundary cannot help: it wraps RouterProvider, and
    // react-router's own RenderErrorBoundary is inside it.
    errorElement: <RouteError />,
    element: <SimpleShell />,
    children: [
      { index: true, element: <TriageFeed /> },
      { path: "calls/:id", element: <CallDetail /> },
      { path: "me", element: <Settings /> },
      { path: "profiles", element: <ProfilesList /> },
      { path: "profiles/new", element: <ProfileNew /> },
      { path: "profiles/:id", element: <ProfileDetail /> },
      { path: "profiles/:id/edit", element: <ProfileEdit /> },
      { path: "boards", element: <BoardsList /> },
      { path: "boards/:id", element: <BoardDetail /> },
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
          {
            path: "golden-set",
            element: (
              <Suspense fallback={<RouteSpinner />}>
                <AdminGoldenSet />
              </Suspense>
            ),
          },
        ],
      },
      // In-app soft 404 only. The server returns index.html for manifest routes
      // alone, so a typed unknown URL hits FastAPI's real 404, never this.
      {
        path: "*",
        // component="main": SimpleShell renders no landmark (every page renders
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
    // THE ROOT ERROR BOUNDARY for this layout (review 2026-10-26 M-fe-2). The
    // three sibling pathless layouts are the roots there are, so each needs its
    // own: react-router renders the DEEPEST matched route that declares an
    // errorElement, and with none declared anywhere a lazy chunk 404 after a
    // deploy replaced the entire app with react-router's developer screen —
    // "Unexpected Application Error! … 💿 Hey developer 👋", no navigation.
    // App.tsx's ErrorBoundary cannot help: it wraps RouterProvider, and
    // react-router's own RenderErrorBoundary is inside it.
    errorElement: <RouteError />,
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
    // THE ROOT ERROR BOUNDARY for this layout (review 2026-10-26 M-fe-2). The
    // three sibling pathless layouts are the roots there are, so each needs its
    // own: react-router renders the DEEPEST matched route that declares an
    // errorElement, and with none declared anywhere a lazy chunk 404 after a
    // deploy replaced the entire app with react-router's developer screen —
    // "Unexpected Application Error! … 💿 Hey developer 👋", no navigation.
    // App.tsx's ErrorBoundary cannot help: it wraps RouterProvider, and
    // react-router's own RenderErrorBoundary is inside it.
    errorElement: <RouteError />,
    // A THIRD pathless layout, sibling to SimpleShell and PreAuthLayout: the
    // full-bleed corridor board. BoardLayout.tsx says why it does not use
    // PreAuthLayout despite §6 asking for that.
    element: <BoardLayout />,
    children: [{ path: "b/:slug", element: <BoardScreen /> }],
  },
]);
