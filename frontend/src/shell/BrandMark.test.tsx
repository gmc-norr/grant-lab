import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { BrandMark } from "./BrandMark";

it("renders an accessible crystal mark at the requested size", () => {
  render(<BrandMark size={24} />);
  const svg = screen.getByLabelText("Call Manager");
  expect(svg).toBeInTheDocument();
  expect(svg.getAttribute("width")).toBe("24");
});
