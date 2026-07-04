import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("./collabProvider", () => ({
  CollabProvider: class {
    on() {
      return () => {};
    }
    destroy() {}
    get status() {
      return "connected";
    }
  },
}));

import { CollabEditor } from "./CollabEditor";

describe("CollabEditor", () => {
  it("renders read-only when canWrite is false", () => {
    render(<CollabEditor workspaceId="w" artifact="draft" canWrite={false} />);
    const el = screen.getByTestId("collab-editor").querySelector("[contenteditable]");
    expect(el?.getAttribute("contenteditable")).toBe("false");
  });
});
