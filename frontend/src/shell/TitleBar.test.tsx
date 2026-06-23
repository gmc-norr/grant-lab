import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { TitleBar } from "./TitleBar";

it("shows the brand and the calls title on the dashboard route", () => {
  renderWithProviders(<TitleBar />, { route: "/" });
  expect(screen.getByText("Call Manager")).toBeInTheDocument();
  expect(screen.getByText("Funding calls")).toBeInTheDocument();
});

it("shows the workspace title on a cockpit route", () => {
  renderWithProviders(<TitleBar />, { route: "/cockpit/abc" });
  expect(screen.getByText("Proposal workspace")).toBeInTheDocument();
});
