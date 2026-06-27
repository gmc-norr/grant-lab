// frontend/src/cockpit/BandBadge.test.tsx
import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { BandBadge } from "./BandBadge";

// renderWithProviders supplies MantineProvider (Badge needs it). Mantine Badge
// nests the label in an inner <span>, so assert data-band on the root via
// querySelector, not on the text node.
it("renders the human label and a stable data-band hook", () => {
  const { container } = renderWithProviders(<BandBadge band="at_risk" />);
  expect(screen.getByText("At-risk")).toBeInTheDocument();
  expect(container.querySelector('[data-band="at_risk"]')).not.toBeNull();
});

it("maps every band to its label", () => {
  const { rerender } = renderWithProviders(<BandBadge band="strong" />);
  expect(screen.getByText("Strong")).toBeInTheDocument();
  rerender(<BandBadge band="gap" />);
  expect(screen.getByText("Gap")).toBeInTheDocument();
});
