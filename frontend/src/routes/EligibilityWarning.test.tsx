import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { theme } from "../theme/theme";
import { EligibilityWarning, eligibilityFactsAreEmpty } from "./EligibilityWarning";

// The verbatim copy, spelled out here so a reworded component reddens this file
// rather than silently agreeing with itself.
const WARNING =
  "Eligibility is not filled in. Calls can't be judged eligible, so no call can score above 50 and none will reach 'top matches' or the board. Fill in organisation type, country and applicant types.";

it("calls a profile empty when organisation type, country and applicant types are all missing", () => {
  expect(eligibilityFactsAreEmpty({ applicantTypes: [], orgType: null, country: null })).toBe(true);
  // What the editor's form state looks like: "" rather than null, and the
  // MultiSelect's newline join of nothing, split back to [] / [""].
  expect(eligibilityFactsAreEmpty({ applicantTypes: [""], orgType: "", country: "   " })).toBe(
    true,
  );
  expect(
    eligibilityFactsAreEmpty({ applicantTypes: [], orgType: undefined, country: undefined }),
  ).toBe(true);
});

it("calls a profile non-empty as soon as ANY ONE of the three facts is filled", () => {
  // One case per fact. These are the directions that stop a predicate hard-wired
  // to `true` -- an alert on every profile forever -- from passing.
  expect(
    eligibilityFactsAreEmpty({ applicantTypes: [], orgType: "university_hospital", country: null }),
  ).toBe(false);
  expect(eligibilityFactsAreEmpty({ applicantTypes: [], orgType: null, country: "SE" })).toBe(
    false,
  );
  expect(
    eligibilityFactsAreEmpty({ applicantTypes: ["solo_pi"], orgType: null, country: null }),
  ).toBe(false);
});

function renderWarning(props: Parameters<typeof EligibilityWarning>[0]) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <EligibilityWarning {...props} />
    </MantineProvider>,
  );
}

it("renders the cap explanation verbatim, as an alert, when the facts are empty", () => {
  renderWarning({ applicantTypes: [], orgType: null, country: null });
  // getByText matches the element's whole normalised text, so this pins the copy
  // -- including the "above 50" and "or the board" clauses, which are the only
  // explanation anywhere in the UI for an empty top-matches list.
  const message = screen.getByText(WARNING);
  expect(message.closest("[role='alert']")).not.toBeNull();
});

it("renders nothing at all when a fact is filled", () => {
  const { container } = renderWarning({
    applicantTypes: [],
    orgType: null,
    country: "SE",
  });
  expect(screen.queryByText(WARNING)).toBeNull();
  expect(container.querySelector("[role='alert']")).toBeNull();
});
