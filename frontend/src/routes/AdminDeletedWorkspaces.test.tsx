import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { theme } from "../theme/theme";
import { AdminDeletedWorkspaces } from "./AdminDeletedWorkspaces";
import { AdminDeletedWorkspaceDetail } from "./AdminDeletedWorkspaceDetail";

// Shared navigate spy — vi.mock runs before imports so we hoist it.
const h = vi.hoisted(() => ({ navigate: vi.fn() }));
vi.mock("react-router-dom", async (orig) => ({
  ...(await orig<typeof import("react-router-dom")>()),
  useNavigate: () => h.navigate,
}));

afterEach(() => {
  vi.restoreAllMocks();
  h.navigate.mockReset();
});

function row(i: number) {
  return {
    id: `1111111${i}-1111-1111-1111-111111111111`,
    title: `Retired grant draft ${i}`,
    owner_email: `owner${i}@ki.se`,
    soft_deleted_at: "2026-08-01T10:00:00Z",
  };
}

// The list page reads BOTH the listing and /api/me (for the retention_inspection
// capability), so the mock has to dispatch on path -- a single mockResolvedValue
// would hand the session payload to the table and vice versa.
function mockList(items: unknown[], total = items.length, { canInspect = true } = {}) {
  return vi.spyOn(clientMod.api, "GET").mockImplementation((async (path: string) => {
    if (path === "/api/me") {
      return {
        data: {
          user: { id: 1, email: "a@ki.se", status: "active", default_profile_id: null },
          capabilities: { admin: true, retention_inspection: canInspect },
        },
        error: undefined,
      };
    }
    return { data: { items, total }, error: undefined };
  }) as never);
}

// Calls to the LISTING endpoint only. The session read is interleaved and its
// ordering is not something these tests should depend on.
function listCalls(get: ReturnType<typeof mockList>) {
  return get.mock.calls.filter((c) => c[0] !== "/api/me");
}

// openapi-fetch's mocked call signature resolves to `never` under vitest's mock
// typing (same reason SelectionToolbar.test.tsx and CitationComposer.test.tsx
// cast `.mock.calls[...][1]` rather than indexing it directly) -- cast the
// request-options arg so `.params.query` is checkable at all.
type ListCall = { params: { query: { limit: number; offset: number } } };

it("lists deleted workspaces with owner and deletion time", async () => {
  mockList([row(0)]);
  renderWithProviders(<AdminDeletedWorkspaces />);
  expect(await screen.findByText("Retired grant draft 0")).toBeInTheDocument();
  expect(screen.getByText("owner0@ki.se")).toBeInTheDocument();
});

it("says so plainly when nothing has been deleted", async () => {
  mockList([], 0);
  renderWithProviders(<AdminDeletedWorkspaces />);
  expect(await screen.findByText(/no deleted workspaces/i)).toBeInTheDocument();
});

it("requests the first page with an explicit offset", async () => {
  const get = mockList([row(0)], 120);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  // Assert the REQUEST, not just the render: a component that renders page 1
  // correctly while never sending offset looks identical until you click Next.
  const params = (listCalls(get)[0][1] as ListCall)?.params?.query;
  expect(params).toMatchObject({ offset: 0 });
  expect(params.limit).toBeGreaterThan(0);
});

it("advances the offset when the admin goes to the next page", async () => {
  const get = mockList([row(0)], 120);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  const limit = (listCalls(get)[0][1] as ListCall).params.query.limit;

  await userEvent.click(screen.getByRole("button", { name: /next/i }));

  await waitFor(() => expect(listCalls(get).length).toBeGreaterThan(1));
  const latest = (listCalls(get).at(-1)![1] as ListCall).params.query;
  expect(latest.offset).toBe(limit);
});

it("does not offer a next page when everything already fits", async () => {
  mockList([row(0)], 1);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  expect(screen.queryByRole("button", { name: /next/i })).toBeNull();
});

// ---- detail page ----

const WS_ID = "11111111-1111-1111-1111-111111111111";

function mockDetail(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: {
      id: WS_ID,
      title: "Retired grant draft",
      owner_email: "owner@ki.se",
      soft_deleted_at: "2026-08-01T10:00:00Z",
      repository_available: true,
      history_available: true,
      proposal_markdown: "# Draft\n\nBody text.\n",
      commits: [
        { sha: "a".repeat(40), authored_at: "2026-07-30T09:00:00Z", subject: "first" },
      ],
      history_truncated: false,
      history_limit: 200,
      ...overrides,
    },
    error: undefined,
  } as never);
}

it("renders the proposal markdown and the commit list", async () => {
  mockDetail();
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/Body text\./)).toBeInTheDocument();
  expect(screen.getByText("first")).toBeInTheDocument();
});

it("renders hostile markdown as text, never as markup", async () => {
  // The draft is written by ordinary users and this page is viewed by an admin.
  // Rendering it as HTML would turn any workspace into stored XSS against the
  // one account that can see every deleted workspace.
  const hostile = '# Title\n\n<img src=x onerror="alert(1)">\n<script>alert(2)</script>\n';
  mockDetail({ proposal_markdown: hostile });
  const { container } = renderWithProviders(<AdminDeletedWorkspaceDetail />);

  // The literal source is visible...
  expect(await screen.findByText(/onerror/)).toBeInTheDocument();
  // ...and no element was created from it.
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.innerHTML).not.toContain("<img");
});

it("states that no repository is readable instead of showing an empty document", async () => {
  mockDetail({ repository_available: false, proposal_markdown: null, commits: [] });
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/no readable repository/i)).toBeInTheDocument();
});

it("says when the history was truncated, using the cap the SERVER sent", async () => {
  // Deliberately not 200: the copy used to hardcode the server's limit, so a
  // change to _HISTORY_LIMIT turned the sentence into a lie with every test
  // still green. A value that is not the real default fails if the number is
  // ever baked into the bundle again.
  mockDetail({ history_truncated: true, history_limit: 37 });
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/most recent 37/i)).toBeInTheDocument();
});

it("re-requests the draft on every open, so each read leaves an audit row", async () => {
  // The backend writes one audit event PER READ and deliberately does not
  // de-duplicate (test_a_second_read_appends_a_second_event). The app-wide
  // client sets staleTime: 20_000 (App.tsx:16), so without a per-query
  // override the second open inside 20s is served from cache: no request, and
  // therefore no second audit row. That would undo a privacy guarantee the
  // backend is careful to make, from the client side.
  //
  // renderWithProviders builds a FRESH QueryClient per call, so it cannot see
  // this at all — the wrapper below reuses one client with the real defaults.
  const get = mockDetail();
  const qc = new QueryClient({
    defaultOptions: { queries: { staleTime: 20_000, refetchOnWindowFocus: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>
  );

  const first = render(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />, { wrapper });
  await screen.findByText(/Body text\./);
  expect(get).toHaveBeenCalledTimes(1);
  first.unmount();

  render(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />, { wrapper });
  await screen.findByText(/Body text\./);
  expect(get).toHaveBeenCalledTimes(2);
});

it("says an unreadable history is unreadable, not empty", async () => {
  // A readable draft proves at least one commit exists, so "No commits." beside
  // one is provably false. The backend distinguishes the two with
  // history_available; the page has to render the difference or the distinction
  // is pointless.
  mockDetail({ history_available: false, commits: [], history_truncated: false });
  renderWithProviders(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />);
  expect(await screen.findByText(/history could not be read/i)).toBeInTheDocument();
  expect(screen.queryByText("No commits.")).toBeNull();
});

it("still says 'No commits.' for a readable but empty history", async () => {
  mockDetail({ history_available: true, commits: [], history_truncated: false });
  renderWithProviders(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />);
  expect(await screen.findByText("No commits.")).toBeInTheDocument();
});


it("does not link a title when reading deleted content is disabled", async () => {
  // The row stays -- seeing WHAT was deleted is never gated -- but the title is
  // not a link, because the detail route would 403 and a door that refuses is
  // worse than no door.
  mockList([row(0)], 1, { canInspect: false });
  renderWithProviders(<AdminDeletedWorkspaces />);

  expect(await screen.findByText("Retired grant draft 0")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Retired grant draft 0" })).toBeNull();
  expect(screen.getByText(/RETENTION_INSPECTION_ENABLED/)).toBeInTheDocument();
});

it("links the title when the capability is granted", async () => {
  // The other half of the pair: without this, deleting the link unconditionally
  // would still pass the test above.
  mockList([row(0)], 1, { canInspect: true });
  renderWithProviders(<AdminDeletedWorkspaces />);

  const link = await screen.findByRole("link", { name: "Retired grant draft 0" });
  expect(link).toHaveAttribute("href", expect.stringContaining("/admin/deleted-workspaces/"));
  expect(screen.queryByText(/RETENTION_INSPECTION_ENABLED/)).toBeNull();
});

// ---- restore action ----

function mockPost(ok = true) {
  if (ok) {
    return vi
      .spyOn(clientMod.api, "POST")
      .mockResolvedValue({ data: undefined, error: undefined } as never);
  }
  return vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: undefined, error: { detail: "Service unavailable" } } as never);
}

// Render the detail page with a caller-supplied QueryClient so we can spy on
// invalidateQueries and track call order.
function renderDetail(qc: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
  render(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />, { wrapper });
}

it("clicking Restore and confirming POSTs to the restore path", async () => {
  mockDetail();
  const post = mockPost();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderWithProviders(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />);
  await userEvent.click(await screen.findByRole("button", { name: /restore workspace/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/admin/workspaces/deleted/{workspace_id}/restore",
      { params: { path: { workspace_id: WS_ID } } },
    ),
  );
});

it("declining the confirm does not POST", async () => {
  mockDetail();
  const post = mockPost();
  vi.spyOn(window, "confirm").mockReturnValue(false);
  renderWithProviders(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />);
  await userEvent.click(await screen.findByRole("button", { name: /restore workspace/i }));
  expect(post).not.toHaveBeenCalled();
});

it("a failed restore shows an error and does not navigate", async () => {
  mockDetail();
  mockPost(false);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderWithProviders(<AdminDeletedWorkspaceDetail workspaceId={WS_ID} />);
  await userEvent.click(await screen.findByRole("button", { name: /restore workspace/i }));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(h.navigate).not.toHaveBeenCalled();
});

it("on success invalidates admin-deleted-workspaces BEFORE navigating", async () => {
  // This test exists to prove the ordering contract: the list inherits a 20s
  // freshness window (App.tsx), so if navigate fires before invalidation the
  // restored row is still on screen when the admin lands back on the list.
  // Remove the invalidateQueries call and this test must go red; if it stays
  // green the test is decorative and the task is failed.
  mockDetail();
  mockPost();
  vi.spyOn(window, "confirm").mockReturnValue(true);

  const qc = new QueryClient();
  const callOrder: string[] = [];
  vi.spyOn(qc, "invalidateQueries").mockImplementation(async () => {
    callOrder.push("invalidate");
  });
  h.navigate.mockImplementation(() => {
    callOrder.push("navigate");
  });

  renderDetail(qc);
  await userEvent.click(await screen.findByRole("button", { name: /restore workspace/i }));
  await waitFor(() => expect(callOrder).toContain("navigate"));
  expect(callOrder).toEqual(["invalidate", "navigate"]);
});

it("Restore works with canInspect: false — regression for ungating", async () => {
  // The list-page Restore must be reachable when RETENTION_INSPECTION_ENABLED
  // is unset (the default). With inspection off the detail page 403s and its
  // link is suppressed — so a button living ONLY on the detail page would be
  // unreachable on every default deployment. This test confirms the list row
  // carries its own Restore control and that it fires the POST.
  mockList([row(0)], 1, { canInspect: false });
  const post = mockPost();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  renderWithProviders(<AdminDeletedWorkspaces />);
  // The title is NOT a link when canInspect is false, but Restore must still
  // be present and functional.
  await screen.findByText("Retired grant draft 0");
  await userEvent.click(screen.getByRole("button", { name: /^restore$/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(
      "/api/admin/workspaces/deleted/{workspace_id}/restore",
      { params: { path: { workspace_id: row(0).id } } },
    ),
  );
});

it("the list page no longer claims nothing here can be restored", async () => {
  mockList([row(0)], 1);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  // The old false copy — must be gone.
  expect(screen.queryByText(/nothing here can be restored/i)).toBeNull();
  // The replacement — content cannot be edited, restore is the only action.
  expect(screen.getByText(/restore is the only action/i)).toBeInTheDocument();
});
