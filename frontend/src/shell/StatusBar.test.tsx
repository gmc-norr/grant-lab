import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { StatusBar } from "./StatusBar";

it("renders the status bar landmark with the app label", () => {
  render(<StatusBar />);
  const bar = screen.getByLabelText("Status bar");
  expect(bar).toBeInTheDocument();
  expect(bar).toHaveTextContent("Call Manager");
});
