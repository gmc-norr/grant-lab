import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Settings } from "./Settings";

afterEach(() => { vi.restoreAllMocks(); });

const SETTINGS = {
  name: "Pär", email: "p@ki.se", is_admin: false,
  google_oidc_enabled: true, google_linked: false, has_password: true,
  profiles: [
    { id: 1, name: "Group A", subscribed: true },
    { id: 2, name: "Group B", subscribed: false },
  ],
};

function mockGet(payload = SETTINGS) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data: payload, error: undefined } as never);
}

it("renders the sections from the settings read-model", async () => {
  mockGet();
  renderWithProviders(<Settings />);
  expect(await screen.findByDisplayValue("Pär")).toBeInTheDocument();
  expect(screen.getByText("p@ki.se")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /change password/i })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /google sign-in/i })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: /Group A/ })).toBeChecked();
  expect(screen.getByRole("checkbox", { name: /Group B/ })).not.toBeChecked();
});

it("hides the password + google sections when the flags are off", async () => {
  mockGet({ ...SETTINGS, has_password: false, google_oidc_enabled: false });
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  expect(screen.queryByRole("heading", { name: /change password/i })).toBeNull();
  expect(screen.queryByRole("heading", { name: /google sign-in/i })).toBeNull();
});

it("saves the display name", async () => {
  mockGet();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { name: "New" }, error: undefined } as never);
  renderWithProviders(<Settings />);
  fireEvent.change(await screen.findByDisplayValue("Pär"), { target: { value: "New" } });
  fireEvent.click(screen.getByRole("button", { name: /save name/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/me/name", { body: { name: "New" } }));
});

it("changes the password and shows success; surfaces an error", async () => {
  mockGet();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: "old" } });
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "newlongpassword" } });
  fireEvent.click(screen.getByRole("button", { name: /change password/i }));
  await waitFor(() => expect(post).toHaveBeenCalledWith("/api/me/password", {
    body: { current_password: "old", new_password: "newlongpassword" },
  }));
  expect(await screen.findByText(/password changed/i)).toBeInTheDocument();
});

it("shows the password error message from the envelope", async () => {
  mockGet();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "bad_request", message: "current password is incorrect" } },
  } as never);
  renderWithProviders(<Settings />);
  await screen.findByDisplayValue("Pär");
  fireEvent.change(screen.getByLabelText(/current password/i), { target: { value: "x" } });
  fireEvent.change(screen.getByLabelText(/new password/i), { target: { value: "newlongpassword" } });
  fireEvent.click(screen.getByRole("button", { name: /change password/i }));
  expect(await screen.findByText(/current password is incorrect/i)).toBeInTheDocument();
});

it("saves subscriptions as the checked profile id set", async () => {
  mockGet();
  const put = vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { subscribed: [1, 2] }, error: undefined } as never);
  renderWithProviders(<Settings />);
  fireEvent.click(await screen.findByRole("checkbox", { name: /Group B/ }));
  fireEvent.click(screen.getByRole("button", { name: /save subscriptions/i }));
  await waitFor(() => expect(put).toHaveBeenCalledWith("/api/me/subscriptions", { body: { profile_ids: [1, 2] } }));
});

it("reconciles subscriptions checked-set from the server response after save", async () => {
  // Scenario: profile 1 is currently subscribed, profile 2 is not. The server
  // returns { subscribed: [2] } (profile 1 was deactivated between load and save).
  // The checkbox state must reflect the server's authoritative answer, not the
  // client's local set.
  mockGet();
  vi.spyOn(clientMod.api, "PUT").mockResolvedValue({ data: { subscribed: [2] }, error: undefined } as never);
  renderWithProviders(<Settings />);
  await screen.findByRole("checkbox", { name: /Group A/ }); // wait for load
  fireEvent.click(screen.getByRole("button", { name: /save subscriptions/i }));
  // After the response, Group A (id=1) should be UNCHECKED (server dropped it)
  // and Group B (id=2) should be CHECKED (server confirmed it).
  await waitFor(() =>
    expect(screen.getByRole("checkbox", { name: /Group A/ })).not.toBeChecked(),
  );
  expect(screen.getByRole("checkbox", { name: /Group B/ })).toBeChecked();
});
