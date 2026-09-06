import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { PublicBoardItemOut } from "../api/publicBoard";
import { theme } from "../theme/theme";
import { BoardCard } from "./BoardCard";

beforeEach(() => {
  // Fixed "today" so deadlineLocalView's day-count is deterministic.
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-01-01T10:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
});

const BASE: PublicBoardItemOut = {
  title: "Genomics Infrastructure Call",
  source_name: "Vinnova",
  deadline_local_date: "2026-01-11", // 10 days out from the frozen "today"
  budget_min: 500_000,
  budget_max: 1_000_000,
  currency: "SEK",
  url: "https://example.org/call/1",
  qr_modules: 29,
  qr_path: "M0 0h1v1h-1z",
};

function renderCard(item: PublicBoardItemOut | null) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <BoardCard item={item} />
    </MantineProvider>,
  );
}

it("renders the QR path as an attribute, not as HTML", () => {
  const { container } = renderCard(BASE);
  const path = container.querySelector("svg path");
  expect(path).not.toBeNull();
  expect(path?.getAttribute("d")).toBe(BASE.qr_path);
  // The payload string never becomes markup: no element it could have
  // injected (an <img>, a <script>) is present, and the path's own
  // "content" as HTML is empty — everything it carries lives on the `d`
  // attribute, which React always treats as a plain string.
  expect(path?.innerHTML).toBe("");
});

it("the viewBox includes a 4-module quiet zone", () => {
  const { container } = renderCard(BASE);
  const svg = container.querySelector("svg");
  expect(svg?.getAttribute("viewBox")).toBe("-4 -4 37 37"); // 29 + 8
});

it("qr_modules === 0 renders no <svg> at all", () => {
  const { container } = renderCard({ ...BASE, qr_modules: 0, qr_path: "" });
  expect(container.querySelector("svg")).toBeNull();
});

it("shows 'Closes in N days' from deadlineLocalView", () => {
  renderCard(BASE);
  expect(screen.getByText("Closes in 10 days")).toBeInTheDocument();
});

it("shows the funder name and the budget", () => {
  renderCard(BASE);
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("500,000 – 1,000,000 SEK")).toBeInTheDocument();
});

it("an empty slot renders no title, funder or QR", () => {
  const { container } = renderCard(null);
  expect(screen.queryByText(BASE.title)).not.toBeInTheDocument();
  expect(container.querySelector("svg")).toBeNull();
});
