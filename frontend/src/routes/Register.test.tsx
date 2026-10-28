import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Register } from "./Register";

afterEach(() => { vi.restoreAllMocks(); });

it("submits email+name and shows the 'request received' state", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: false, allowed_domains: ["umu.se"] }, error: undefined,
  } as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderWithProviders(<Register />, { route: "/register" });
  await userEvent.type(screen.getByLabelText(/email/i), "new@umu.se");
  await userEvent.click(screen.getByRole("button", { name: /request access/i }));
  await waitFor(() => expect(screen.getByText(/request received/i)).toBeInTheDocument());
  expect(post).toHaveBeenCalledWith("/api/auth/register", { body: { email: "new@umu.se", name: null } });
});

it("a failed request access shows an alert, not a silently re-enabled button", async () => {
  // Review 2026-10-26: "Register on a 500: alerts=[], and the button just
  // re-enables." The member has no way to tell a recorded request from a lost
  // one — and the success screen is deliberately indistinguishable, so silence
  // reads as success.
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: false, allowed_domains: ["umu.se"] }, error: undefined,
  } as never);
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "internal_error", message: "the mailer is down" } },
    response: new Response(null, { status: 500 }),
  } as never);
  renderWithProviders(<Register />, { route: "/register" });
  await userEvent.type(screen.getByLabelText(/email/i), "new@umu.se");
  await userEvent.click(screen.getByRole("button", { name: /request access/i }));
  const alert = await screen.findByRole("alert");
  await waitFor(() => expect(alert).toHaveTextContent(/couldn.t|could not|try again/i));
  // ...and NOT the success screen, which would read as a recorded request.
  expect(screen.queryByText(/request received/i)).not.toBeInTheDocument();
});
