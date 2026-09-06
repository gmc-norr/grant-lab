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

// MINOR 4 fix: the old name ("renders the QR path as an attribute, not as
// HTML") asserted `getAttribute("d")` plus an empty `innerHTML` on the
// <path> — both would look IDENTICAL under a `dangerouslySetInnerHTML`
// implementation, since a rendered <path> has no children either way. What
// actually tells the two apart: a value containing an unescaped `"`. A JSX
// `d={qr_path}` expression always sets the exact literal string via the DOM
// attribute API, which never re-parses it as markup. A
// `dangerouslySetInnerHTML` built from a template string like
// `<path d="${qr_path}">` would instead let the browser's HTML parser treat
// the embedded `"` as closing the attribute early, turning the rest of the
// payload into a REAL new attribute on the element (e.g. an event handler).
it("renders the QR path as a literal attribute value, not parsed HTML", () => {
  const maliciousPath = 'M0 0h1v1h-1z" onmouseover="window.__pwned = true';
  const { container } = renderCard({ ...BASE, qr_path: maliciousPath });
  const path = container.querySelector("svg path");
  expect(path).not.toBeNull();
  // The FULL literal string survives, quote included — an HTML parser would
  // have truncated it at the embedded `"` instead.
  expect(path?.getAttribute("d")).toBe(maliciousPath);
  expect(path?.getAttribute("onmouseover")).toBeNull();
  expect(container.querySelector("[onmouseover]")).toBeNull();
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

// MINOR 1 fix: a deadline dated today is still open (days === 0) — the most
// urgent case on the whole board — and used to render the literal string
// "Closes in 0 days".
it("shows 'Closes today' when the deadline is today, not 'Closes in 0 days'", () => {
  renderCard({ ...BASE, deadline_local_date: "2026-01-01" }); // frozen "today"
  expect(screen.getByText("Closes today")).toBeInTheDocument();
  expect(screen.queryByText("Closes in 0 days")).not.toBeInTheDocument();
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
