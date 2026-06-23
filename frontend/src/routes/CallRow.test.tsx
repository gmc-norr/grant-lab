import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import type { CallItem } from "../api/dashboard";
import { CallRow } from "./CallRow";

function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 3,
    title: "Grant X",
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: null,
    published_at: null,
    aggregate_score: 82,
    ...over,
  } as CallItem;
}

it("links the title to the Jinja call-detail page, shows source + match badge", () => {
  render(<CallRow call={call()} />);
  expect(screen.getByRole("link", { name: "Grant X" })).toHaveAttribute("href", "/calls/3");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("omits the match badge when the score is null", () => {
  render(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});
