import { Button } from "@mantine/core";
import { screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { renderWithProviders } from "../test-utils";
import { theme } from "./theme";

it("uses the teal primary palette with 10 shades", () => {
  expect(theme.primaryColor).toBe("teal");
  expect(theme.colors?.teal).toHaveLength(10);
  expect(theme.colors?.teal?.[5]).toBe("#2dd4bf");
  expect(theme.colors?.teal?.[6]).toBe("#14b8a6");
});

it("renders Mantine components through the provider in test-utils", () => {
  renderWithProviders(<Button>Save</Button>);
  const btn = screen.getByRole("button", { name: "Save" });
  expect(btn.className).toMatch(/mantine-Button/);
});
