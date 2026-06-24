import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import { renderWithProviders } from "../test-utils";
import { WorkspaceCreate } from "./WorkspaceCreate";

afterEach(() => {
  vi.restoreAllMocks();
});

function stubLocation() {
  const orig = window.location;
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    value: { ...orig, assign },
  });
  return { assign, restore: () => Object.defineProperty(window, "location", { configurable: true, value: orig }) };
}

it("prefills the title from ?call_title and creates, then hands off to Jinja setup", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { id: "new-9" }, error: undefined } as never);
  const loc = stubLocation();
  try {
    const user = userEvent.setup();
    renderWithProviders(<WorkspaceCreate />, {
      route: "/cockpit/new?call_id=3&call_title=Grant%20X&profile_id=7",
    });
    const title = screen.getByLabelText("Title") as HTMLInputElement;
    expect(title.value).toBe("Grant X");
    await user.click(screen.getByRole("button", { name: "Start drafting" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith(
        "/api/workspaces",
        expect.objectContaining({ body: expect.objectContaining({ call_id: 3, title: "Grant X", profile_id: 7 }) }),
      ),
    );
    await waitFor(() => expect(loc.assign).toHaveBeenCalledWith("/workspaces/new-9"));
  } finally {
    loc.restore();
  }
});

it("shows a guard (not a form) when call_id is missing", () => {
  renderWithProviders(<WorkspaceCreate />, { route: "/cockpit/new" });
  expect(screen.getByText(/pick a call from the dashboard/i)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Start drafting" })).toBeNull();
});
