import { fireEvent, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { renderWithProviders } from "../test-utils";
import { RubricEditor } from "./RubricEditor";

const CRITERIA = [
  { name: "Impact", description: "d", weight: 3, scale_max: 5, subcriteria: [{ name: "reach" }] },
  { name: "Feasibility", description: "d", weight: 1, scale_max: 5 },
];

it("shows normalized weight % and preserves passthrough fields on edit", () => {
  const onChange = vi.fn();
  renderWithProviders(<RubricEditor criteria={CRITERIA as never} funderNotes="" onChange={onChange} />);
  // 3 / (3+1) = 75%
  expect(screen.getByText(/75\s*%/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(/weight for Impact/i), { target: { value: "5" } });
  const next = onChange.mock.calls[0][0];
  expect(next.criteria[0].weight).toBe(5);
  expect(next.criteria[0].subcriteria).toEqual([{ name: "reach" }]); // passthrough preserved
});
