import { expect, it } from "vitest";

import { resolveEdition } from "./edition";

it("defaults to full when the flag is unset", () => {
  expect(resolveEdition(undefined)).toBe("full");
  expect(resolveEdition("")).toBe("full");
});

it("returns match only for the exact string", () => {
  expect(resolveEdition("match")).toBe("match");
});

it("falls back to full for anything unrecognised", () => {
  // A typo must not silently ship the cockpit-excluded build to everyone, and
  // must not silently ship the cockpit into a match deployment either. Full is
  // the safe default: it is the superset, so nothing 404s.
  expect(resolveEdition("Match")).toBe("full");
  expect(resolveEdition("simple")).toBe("full");
  expect(resolveEdition("true")).toBe("full");
});
