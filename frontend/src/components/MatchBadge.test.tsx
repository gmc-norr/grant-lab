import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { MatchBadge } from "./MatchBadge";

it("rounds and bands at the 80 / 60 boundaries", () => {
  const { rerender } = render(<MatchBadge score={82.4} />);
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
  rerender(<MatchBadge score={80} />);
  expect(screen.getByText("80")).toHaveAttribute("data-band", "strong");
  rerender(<MatchBadge score={79} />);
  expect(screen.getByText("79")).toHaveAttribute("data-band", "good");
  rerender(<MatchBadge score={60} />);
  expect(screen.getByText("60")).toHaveAttribute("data-band", "good");
  rerender(<MatchBadge score={59} />);
  expect(screen.getByText("59")).toHaveAttribute("data-band", "low");
});

it("renders nothing for a null score", () => {
  const { container } = render(<MatchBadge score={null} />);
  expect(container).toBeEmptyDOMElement();
});
