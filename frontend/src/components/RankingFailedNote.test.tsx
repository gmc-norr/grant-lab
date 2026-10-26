import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { RankingFailedNote } from "./RankingFailedNote";

// The copy is the spec: "Couldn't rank this call with the local model (last
// tried <date>)". Asserted as ONE whole string rather than two substrings --
// a note that printed the sentence without the date, or the date without the
// sentence, is the half-fix this line exists to prevent.
it("prints the sentence and the date it last tried", () => {
  render(<RankingFailedNote failed at="2026-10-25T03:17:42Z" />);
  const note = screen.getByText(
    "Couldn't rank this call with the local model (last tried 2026-10-25)",
  );
  expect(note).toHaveAttribute("data-field", "ranking-failed");
});

it("renders nothing at all for a pair that ranks fine", () => {
  // Not "renders an empty element": the healthy case is almost every row in the
  // feed, and it must cost no vertical space.
  const { container } = render(<RankingFailedNote failed={false} at={null} />);
  expect(container).toBeEmptyDOMElement();
});

it("still prints the sentence when the API sent no timestamp", () => {
  // The API's invariant is that `ranking_failed_at` is non-null exactly when
  // `ranking_failed` is true, so this is a guard against a future shape -- but a
  // literal "(last tried null)" on a researcher's card would be worse than the
  // missing clause.
  render(<RankingFailedNote failed at={null} />);
  expect(
    screen.getByText("Couldn't rank this call with the local model"),
  ).toBeInTheDocument();
  expect(screen.queryByText(/last tried/)).not.toBeInTheDocument();
});
