import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { ProfileTransferCard } from "./ProfileTransferCard";

function LocationProbe() {
  return <span data-testid="loc">{useLocation().pathname}</span>;
}

afterEach(() => {
  vi.restoreAllMocks();
});

const OWNER_PATH = "/api/profiles/{profile_id}/owner";
const PARAMS = { params: { path: { profile_id: 5 } } };

/** Mount the card directly on the two fields `GET /api/profiles/{id}` already
 *  supplies -- `outgoingLostRead` is a PROP here on purpose (D17): the real
 *  ProfileDetail reads it off the profile-detail payload it is already
 *  holding, never a second fetch this card would own. */
function renderCard({
  outgoingLostRead = false,
  putResult,
}: {
  outgoingLostRead?: boolean;
  putResult?: unknown;
} = {}) {
  const put = vi
    .spyOn(clientMod.api, "PUT")
    .mockImplementation(
      (async () =>
        putResult ?? {
          data: { owner: { user_id: 9, name: "Bob", email: "bob@umu.se" }, outgoing_lost_read: false },
          error: undefined,
        }) as never,
    );
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <MemoryRouter initialEntries={["/profiles/5"]}>
          <ProfileTransferCard profileId={5} outgoingLostRead={outgoingLostRead} />
          <LocationProbe />
        </MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>,
  );
  return { put };
}

it("warns a non-admin owner that they will lose read access", () => {
  renderCard({ outgoingLostRead: true });
  expect(screen.getByRole("alert")).toHaveTextContent(/lose (read )?access/i);
});

// Gate A: a hardcoded "you are the owner" sentence would also pass the test
// above. This is the one that catches it -- the field itself already answers
// "would an admin owner lose anything" (transfer_read_effect, profiles.py),
// so the card must render exactly what it is given, not re-derive "is owner".
it("does NOT warn an admin owner, because they lose nothing", () => {
  renderCard({ outgoingLostRead: false });
  expect(screen.queryByRole("alert")).toBeNull();
});

it("keep-read-access is off by default", () => {
  renderCard();
  const checkbox = screen.getByRole("checkbox", { name: /keep.*read access/i });
  expect(checkbox).not.toBeChecked();
});

it("sends keep_read_access=true only once the box is checked", async () => {
  const user = userEvent.setup();
  const { put } = renderCard({ outgoingLostRead: true });
  await user.type(screen.getByLabelText(/email/i), "bob@umu.se");
  await user.click(screen.getByRole("checkbox", { name: /keep.*read access/i }));
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  expect(put).toHaveBeenCalledWith(OWNER_PATH, {
    ...PARAMS,
    body: { email: "bob@umu.se", keep_read_access: true },
  });
});

it("shows one generic error for an unknown and for an inactive address", async () => {
  const user = userEvent.setup();
  const errorResult = {
    data: undefined,
    error: { error: { code: "bad_request", message: "could not transfer profile" } },
  };
  const { put } = renderCard({ putResult: errorResult });
  await user.type(screen.getByLabelText(/email/i), "unknown@umu.se");
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  const unknownMessage = await screen.findByText(/couldn't transfer/i);

  put.mockClear();
  await user.clear(screen.getByLabelText(/email/i));
  await user.type(screen.getByLabelText(/email/i), "inactive@umu.se");
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  const inactiveMessage = await screen.findByText(/couldn't transfer/i);

  // Same wording either way -- the server already collapses "unknown" and
  // "inactive" into one refusal (Open-8); this card must not invent a more
  // specific message that would turn it into a directory of who has an account.
  expect(unknownMessage.textContent).toBe(inactiveMessage.textContent);
  expect(screen.queryByText(/no such user/i)).toBeNull();
  expect(screen.queryByText(/inactive/i)).toBeNull();
});

it("navigates an OWNER away when the transfer really cost them their read access", async () => {
  const user = userEvent.setup();
  renderCard({
    // The GET prop is true only for a caller who IS the outgoing owner --
    // transfer_read_effect returns False when profile.created_by != outgoing.id.
    outgoingLostRead: true, // the PRE-act hypothetical, same as the realized outcome here
    putResult: {
      data: { owner: { user_id: 9, name: "Bob", email: "bob@umu.se" }, outgoing_lost_read: true },
      error: undefined,
    },
  });
  await user.type(screen.getByLabelText(/email/i), "bob@umu.se");
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  // Staying on THIS page would refetch straight into "not shared with you" --
  // the REALIZED field (the PUT response), not the pre-act prop, drives this.
  expect(await screen.findByTestId("loc")).toHaveTextContent(/^\/profiles$/);
});

it("stays put when keep_read_access preserved the caller's own read access", async () => {
  const user = userEvent.setup();
  renderCard({
    outgoingLostRead: true, // hypothetical WITHOUT keep_read_access
    putResult: {
      data: { owner: { user_id: 9, name: "Bob", email: "bob@umu.se" }, outgoing_lost_read: false },
      error: undefined,
    },
  });
  await user.type(screen.getByLabelText(/email/i), "bob@umu.se");
  await user.click(screen.getByRole("checkbox", { name: /keep.*read access/i }));
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  await screen.findByText(/transferred to bob@umu.se/i);
  expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/5");
});


it("does NOT navigate an admin away, though the PREVIOUS owner lost read", async () => {
  // A whole-branch review found the bug this pins. `outgoing_lost_read` on the
  // PUT response describes the PREVIOUS OWNER, not the caller: the server
  // computes it for `session.get(User, p.created_by)` read before the
  // ownership write. So an admin transferring somebody else's private profile
  // gets `true` -- the previous owner lost read, the admin lost nothing and
  // still reads it through the admin arm -- and navigating on that field alone
  // yanked them off a page they can perfectly well see.
  //
  // The GET prop is the discriminator: false for a non-owner caller. Both
  // conjuncts together mean "the caller IS the outgoing owner AND really lost it".
  const user = userEvent.setup();
  renderCard({
    outgoingLostRead: false, // admin caller: the pre-act hypothetical costs them nothing
    putResult: {
      data: { owner: { user_id: 9, name: "Bob", email: "bob@umu.se" }, outgoing_lost_read: true },
      error: undefined,
    },
  });
  await user.type(screen.getByLabelText(/email/i), "bob@umu.se");
  await user.click(screen.getByRole("button", { name: /transfer/i }));
  await screen.findByText(/transferred to bob@umu.se/i);
  expect(screen.getByTestId("loc")).toHaveTextContent("/profiles/5");
});

it("labels the keep-read checkbox for the CURRENT OWNER, not the caller", async () => {
  // The server writes the ProfileViewer row for `outgoing` -- the current
  // owner. Under an admin caller "Keep my own read access" grants the row to a
  // third party and does nothing for the admin, who already reads everything.
  renderCard();
  const label = screen.getByRole("checkbox", { name: /keep.*read access/i });
  expect(label).toBeTruthy();
  expect(screen.queryByRole("checkbox", { name: /my own/i })).toBeNull();
});
