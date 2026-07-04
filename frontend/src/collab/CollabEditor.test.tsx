import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";

vi.mock("./collabProvider", () => ({
  CollabProvider: class {
    on() {
      return () => {};
    }
    destroy() {}
    isSettled() {
      return false;
    }
    get status() {
      return "connected";
    }
  },
}));

vi.mock("./useSaveVersion", () => ({
  useSaveVersion: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { CollabEditor } from "./CollabEditor";

describe("CollabEditor", () => {
  it("renders read-only when canWrite is false", () => {
    renderWithProviders(<CollabEditor workspaceId="w" artifact="draft" canWrite={false} />);
    const el = screen.getByTestId("collab-editor").querySelector("[contenteditable]");
    expect(el?.getAttribute("contenteditable")).toBe("false");
  });
});
