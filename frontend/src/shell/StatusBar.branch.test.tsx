import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { StatusBar } from "./StatusBar";

const useBranchesSpy = vi.fn(() => ({ data: { branches: ["main", "v2"], current: "v2" } }));
// eslint-disable-next-line @typescript-eslint/no-unsafe-argument
vi.mock("../api/branches", () => ({ useBranches: (...a: unknown[]) => useBranchesSpy(...(a as Parameters<typeof useBranchesSpy>)) }));

beforeEach(() => useBranchesSpy.mockClear());

describe("StatusBar branch", () => {
  it("names the branch beside the commit on the cockpit", () => {
    render(
      <MemoryRouter initialEntries={["/cockpit/w1?tab=draft"]}>
        <StatusBar />
      </MemoryRouter>,
    );
    expect(screen.getByText(/v2/)).toBeInTheDocument();
  });

  it("does not FETCH branches off the cockpit", () => {
    // Asserting the text is absent on /settings would pass before the feature
    // exists — an absence that is already true is not a gate. Assert the wiring
    // seam instead: the query must be disabled, which is the same
    // `enabled: !!id` discipline the rest of this bar already uses.
    render(
      <MemoryRouter initialEntries={["/settings"]}>
        <StatusBar />
      </MemoryRouter>,
    );
    expect(useBranchesSpy).toHaveBeenCalledWith(null, { enabled: false });
  });
});
