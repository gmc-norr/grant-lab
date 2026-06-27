import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { Register } from "./Register";

afterEach(() => { vi.restoreAllMocks(); });

it("submits email+name and shows the 'request received' state", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: { google_oidc_enabled: false, allowed_domains: ["ki.se"] }, error: undefined,
  } as never);
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { ok: true }, error: undefined } as never);
  renderWithProviders(<Register />, { route: "/register" });
  await userEvent.type(screen.getByLabelText(/email/i), "new@ki.se");
  await userEvent.click(screen.getByRole("button", { name: /request access/i }));
  await waitFor(() => expect(screen.getByText(/request received/i)).toBeInTheDocument());
  expect(post).toHaveBeenCalledWith("/api/auth/register", { body: { email: "new@ki.se", name: null } });
});
