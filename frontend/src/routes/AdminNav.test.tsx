import { screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import { render } from "@testing-library/react";
import { AdminNav } from "./AdminNav";

it("renders all admin section links", () => {
  render(<MemoryRouter><AdminNav /></MemoryRouter>);
  for (const label of ["Operational health", "Drafting", "Golden set", "Users", "Pending"]) {
    expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: "Operational health" })).toHaveAttribute("href", "/admin");
  expect(screen.getByRole("link", { name: "Golden set" })).toHaveAttribute("href", "/admin/golden-set");
});
