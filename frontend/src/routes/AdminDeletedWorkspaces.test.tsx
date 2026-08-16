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

afterEach(() => {
  vi.restoreAllMocks();
});

function row(i: number) {
  return {
    id: `1111111${i}-1111-1111-1111-111111111111`,
    title: `Retired grant draft ${i}`,
    owner_email: `owner${i}@ki.se`,
    soft_deleted_at: "2026-08-01T10:00:00Z",
  };
}

function mockList(items: unknown[], total = items.length) {
  return vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { items, total }, error: undefined } as never);
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
  const params = (get.mock.calls[0][1] as ListCall)?.params?.query;
  expect(params).toMatchObject({ offset: 0 });
  expect(params.limit).toBeGreaterThan(0);
});

it("advances the offset when the admin goes to the next page", async () => {
  const get = mockList([row(0)], 120);
  renderWithProviders(<AdminDeletedWorkspaces />);
  await screen.findByText("Retired grant draft 0");
  const limit = (get.mock.calls[0][1] as ListCall).params.query.limit;

  await userEvent.click(screen.getByRole("button", { name: /next/i }));

  await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1));
  const latest = (get.mock.calls.at(-1)![1] as ListCall).params.query;
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
      proposal_markdown: "# Draft\n\nBody text.\n",
      commits: [
        { sha: "a".repeat(40), authored_at: "2026-07-30T09:00:00Z", subject: "first" },
      ],
      history_truncated: false,
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

it("says when the history was truncated", async () => {
  mockDetail({ history_truncated: true });
  renderWithProviders(<AdminDeletedWorkspaceDetail />);
  expect(await screen.findByText(/most recent 200/i)).toBeInTheDocument();
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
