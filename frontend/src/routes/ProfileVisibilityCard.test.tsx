import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileVisibilityCard } from "./ProfileVisibilityCard";

afterEach(() => {
  vi.restoreAllMocks();
});

const ADA = { user_id: 7, name: "Ada Lovelace", email: "ada@umu.se" };

const VIEWERS_PATH = "/api/profiles/{profile_id}/viewers";
const VISIBILITY_PATH = "/api/profiles/{profile_id}/visibility";
const PARAMS = { params: { path: { profile_id: 5 } } };

/** Mount the card with every endpoint it can reach stubbed. `PUT` is routed by
 *  path so a test can tell a viewer-list write apart from a visibility write —
 *  the whole point of gate 18. */
function renderCard({
  visibility = "private",
  viewers = [] as (typeof ADA)[],
  addResult,
}: {
  visibility?: string;
  viewers?: (typeof ADA)[];
  addResult?: unknown;
} = {}) {
  const get = vi
    .spyOn(clientMod.api, "GET")
    .mockResolvedValue({ data: { viewers }, error: undefined } as never);
  const put = vi.spyOn(clientMod.api, "PUT").mockImplementation((async (path: string) =>
    path === VISIBILITY_PATH
      ? { data: { visibility: "org" }, error: undefined }
      : { data: { viewers: [] }, error: undefined }) as never);
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue(
      (addResult ?? { data: { viewers: [ADA] }, error: undefined }) as never,
    );
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <ProfileVisibilityCard profileId={5} visibility={visibility} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { get, put, post };
}

it("shows the current visibility and, when private, who it is shared with", async () => {
  const { get } = renderCard({ viewers: [ADA] });
  expect(screen.getByRole("radio", { name: "Only me and admins" })).toBeChecked();
  expect(await screen.findByText("ada@umu.se")).toBeInTheDocument();
  // Names, not ids: a list of `[7]` is unusable.
  expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
  expect(get).toHaveBeenCalledWith(VIEWERS_PATH, PARAMS);
});

it("asks for no viewer list while the profile is org-visible", async () => {
  const { get } = renderCard({ visibility: "org" });
  expect(screen.getByRole("radio", { name: "Everyone here" })).toBeChecked();
  expect(screen.queryByLabelText(/share with/i)).toBeNull();
  await waitFor(() => expect(get).not.toHaveBeenCalled());
});

// --- gate 16 -------------------------------------------------------------
// Both directions, deliberately: a control that sent `org` for BOTH positions
// (V25) would satisfy a one-direction gate.

it("gate 16: choosing 'Everyone here' PUTs visibility=org", async () => {
  const user = userEvent.setup();
  const { put } = renderCard({ visibility: "private" });
  // Mantine's SegmentedControl hides its radio input; userEvent must click the LABEL.
  await user.click(screen.getByText("Everyone here"));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(VISIBILITY_PATH, { ...PARAMS, body: { visibility: "org" } }),
  );
});

it("gate 16: choosing 'Only me and admins' PUTs visibility=private", async () => {
  const user = userEvent.setup();
  const { put } = renderCard({ visibility: "org" });
  await user.click(screen.getByText("Only me and admins"));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(VISIBILITY_PATH, {
      ...PARAMS,
      body: { visibility: "private" },
    }),
  );
  expect(put).not.toHaveBeenCalledWith(VISIBILITY_PATH, { ...PARAMS, body: { visibility: "org" } });
});

// --- gate 18 -------------------------------------------------------------

it("gate 18: removing the last viewer leaves the profile private", async () => {
  const user = userEvent.setup();
  const { put } = renderCard({ visibility: "private", viewers: [ADA] });
  await screen.findByText("ada@umu.se");
  await user.click(screen.getByRole("button", { name: "Remove ada@umu.se" }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(VIEWERS_PATH, { ...PARAMS, body: { user_ids: [] } }),
  );
  // The list reconciles from the server's answer…
  await waitFor(() => expect(screen.queryByText("ada@umu.se")).toBeNull());
  // …and nothing re-shared the profile behind the owner's back.
  expect(put).not.toHaveBeenCalledWith(VISIBILITY_PATH, expect.anything());
  expect(screen.getByRole("radio", { name: "Only me and admins" })).toBeChecked();
  expect(screen.getByRole("radio", { name: "Everyone here" })).not.toBeChecked();
});

it("removing one of two viewers keeps the other", async () => {
  const user = userEvent.setup();
  const BOB = { user_id: 9, name: "Bob", email: "bob@umu.se" };
  const { put } = renderCard({ viewers: [ADA, BOB] });
  await screen.findByText("ada@umu.se");
  await user.click(screen.getByRole("button", { name: "Remove ada@umu.se" }));
  await waitFor(() =>
    expect(put).toHaveBeenCalledWith(VIEWERS_PATH, { ...PARAMS, body: { user_ids: [9] } }),
  );
});

// --- the add gate --------------------------------------------------------

it("add gate: adding a viewer POSTs the email and renders the server's list", async () => {
  const user = userEvent.setup();
  const { post } = renderCard({ viewers: [] });
  await user.type(screen.getByLabelText(/share with/i), "ada@umu.se");
  await user.click(screen.getByRole("button", { name: "Add" }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith(VIEWERS_PATH, { ...PARAMS, body: { email: "ada@umu.se" } }),
  );
  expect(await screen.findByText("ada@umu.se")).toBeInTheDocument();
});

it("a rejected address gets one generic message, never a user-existence answer", async () => {
  const user = userEvent.setup();
  renderCard({
    viewers: [],
    addResult: {
      data: undefined,
      error: { error: { code: "bad_request", message: "could not add viewer" } },
    },
  });
  await user.type(screen.getByLabelText(/share with/i), "nobody@umu.se");
  await user.click(screen.getByRole("button", { name: "Add" }));
  expect(await screen.findByText(/couldn't add that address/i)).toBeInTheDocument();
  expect(screen.queryByText(/no such user/i)).toBeNull();
  expect(screen.queryByText(/does not exist/i)).toBeNull();
});
