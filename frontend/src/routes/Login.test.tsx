import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { ApiError } from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Login } from "./Login";

afterEach(() => { vi.restoreAllMocks(); });

function mockConfig(enabled: boolean) {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: enabled, allowed_domains: ["umu.se"] }, error: undefined,
  } as never);
}

it("renders email/password and the 'Request access' link; no Google button when disabled", async () => {
  mockConfig(false);
  renderWithProviders(<Login />, { route: "/login" });
  expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
  expect(screen.getByLabelText(/password/i, { selector: "input" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /request access/i })).toHaveAttribute("href", "/register");
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: /continue with google/i })).not.toBeInTheDocument(),
  );
});

it("shows a native Google POST form when config enables it", async () => {
  mockConfig(true);
  renderWithProviders(<Login />, { route: "/login" });
  const btn = await screen.findByRole("button", { name: /continue with google/i });
  expect(btn.closest("form")).toHaveAttribute("action", "/auth/google/start");
  expect(btn.closest("form")).toHaveAttribute("method", "post");
});

it("on success invalidates ['me'] and navigates to the server redirect", async () => {
  mockConfig(false);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: { redirect: "/calls/5" }, error: undefined, response: new Response(),
  } as never);
  renderWithProviders(<Login />, { route: "/login?next=/calls/5" });
  await userEvent.type(screen.getByLabelText(/email/i), "a@umu.se");
  await userEvent.type(screen.getByLabelText(/password/i, { selector: "input" }), "correct-horse-1");
  await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
  // threads ?next into the POST
  await waitFor(() =>
    expect(clientMod.api.POST).toHaveBeenCalledWith("/api/auth/login", {
      body: { email: "a@umu.se", password: "correct-horse-1", next: "/calls/5" },
    }),
  );
});

it("shows ONE generic message for every failure mode (anti-enumeration)", async () => {
  mockConfig(false);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    error: { error: { code: "bad_request", message: "Invalid email or password." } },
    response: new Response(null, { status: 400 }),
  } as never);
  renderWithProviders(<Login />, { route: "/login" });
  await userEvent.type(screen.getByLabelText(/email/i), "ghost@umu.se");
  await userEvent.type(screen.getByLabelText(/password/i, { selector: "input" }), "whatever-long");
  await userEvent.click(screen.getByRole("button", { name: /^sign in$/i }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Invalid email or password.");
  expect(alert).not.toHaveTextContent("bad_request"); // code prefix stripped
});

it("ApiError message strips to the bare server message", () => {
  // Pins the strip helper contract used by the component.
  const e = new ApiError("bad_request", "Invalid email or password.");
  expect(e.userMessage).toBe("Invalid email or password.");
});

it("says what to do about a forgotten password, since there is no reset link", () => {
  // UX pass 2026-10-31: the page offered nothing at all. There is no
  // self-service reset; an admin sets a new one from Admin > Users.
  mockConfig(false);
  renderWithProviders(<Login />, { route: "/login" });
  expect(screen.getByText("Forgot your password? Ask an admin to reset it.")).toBeInTheDocument();
});
