import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { Button } from "./Button";

it("renders its label and handles disabled", () => {
  render(<Button disabled>Save</Button>);
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
});
