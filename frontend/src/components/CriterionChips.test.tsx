import { readFileSync } from "node:fs";

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "../api/schema.gen";

import { CRITERIA, CriterionChips } from "./CriterionChips";

// Plain `render`, not `renderWithProviders` (frontend/src/test-utils.tsx), and
// deliberately: this component uses no Mantine, router or query primitive, and
// MantineProvider injects two <style> elements INTO the container, which makes
// the zero-criteria guarantee below ("renders nothing at all") unassertable.
// Same call as its neighbour MatchBadge.test.tsx, which pins the same shape.

type Criterion = components["schemas"]["CallListCriterionOut"];

// The API's own shape, filled in per test. `label`, `is_topic_fit` and
// `is_reason_code` are all SERVER-computed (web/api/calls.py::
// _criterion_common_fields, via web/filters.py::criterion_label), so these
// fixtures carry what the backend really sends rather than inventing it — the
// component derives none of the three.
function crit(over: Partial<Criterion> & { criterion: string; rating: string }): Criterion {
  return {
    label: over.criterion,
    is_topic_fit: over.criterion === "topic_fit",
    is_reason_code: ["budget", "timeline", "trl", "embedding"].includes(over.criterion),
    rationale: null,
    quoted_evidence: null,
    ...over,
  };
}

// One row of every criterion the backend can write, in the order the API sends
// them (D5: `ORDER BY criterion`, i.e. alphabetical) — deliberately NOT the
// display order, so the ordering assertion has something to do.
const ALL: Criterion[] = [
  crit({ criterion: "budget", rating: "pass", label: "Budget" }),
  crit({ criterion: "eligibility", rating: "unclear", label: "Eligibility" }),
  crit({ criterion: "embedding", rating: "pass", label: "Topic similarity" }),
  crit({ criterion: "exclusions", rating: "pass", label: "Exclusions" }),
  crit({ criterion: "topic_fit", rating: "4", label: "Topic fit" }),
  crit({ criterion: "trl", rating: "unknown", label: "Technology Readiness Level" }),
];

function chips(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>("[data-criterion]")];
}

describe("how many chips", () => {
  it("renders one chip per criterion for a full six-criterion row, in reading order", () => {
    // B20 killer: `criteria.slice(0, 6)` renders six.
    const { container } = render(<CriterionChips criteria={ALL} />);
    expect(chips(container)).toHaveLength(6);
    expect(chips(container).map((el) => el.dataset.criterion)).toEqual([...CRITERIA]);
  });

  it("renders a single chip, because a gate FAIL short-circuits the rest", () => {
    // ranking/worker.py returns on the first FAIL, so a one-chip row is normal
    // data, not a degenerate case (D-B3).
    const { container } = render(
      <CriterionChips criteria={[crit({ criterion: "budget", rating: "fail", label: "Budget" })]} />,
    );
    expect(chips(container)).toHaveLength(1);
    expect(chips(container)[0]).toHaveTextContent("Budget ✗");
    expect(chips(container)[0].dataset.tone).toBe("fail");
  });

  it("renders nothing at all for zero criteria — not an empty row", () => {
    const { container } = render(<CriterionChips criteria={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("rating -> tone and text", () => {
  it("tones pass and fail, and shows a symbol for each", () => {
    const { container } = render(
      <CriterionChips
        criteria={[
          crit({ criterion: "eligibility", rating: "pass", label: "Eligibility" }),
          crit({ criterion: "exclusions", rating: "fail", label: "Exclusions" }),
        ]}
      />,
    );
    const [pass, fail] = chips(container);
    expect(pass).toHaveTextContent("Eligibility ✓");
    expect(pass.dataset.tone).toBe("pass");
    expect(fail).toHaveTextContent("Exclusions ✗");
    expect(fail.dataset.tone).toBe("fail");
  });

  it("gives BOTH not-judged ratings the unsure tone and the '?' symbol", () => {
    // B19 killer. `unknown` is GateStatus.UNKNOWN (ranking/criteria.py) and
    // `unclear` is the LLM's eligibility verdict; spec §4.2 mentions neither.
    // Dropping either from TONE does NOT change the tone — the
    // unrecognised-rating fallback is `unsure` too — it changes the TEXT, from
    // the '?' symbol to the raw word. So this asserts the text, not just the tone.
    const { container } = render(
      <CriterionChips
        criteria={[
          crit({ criterion: "trl", rating: "unknown", label: "Technology Readiness Level" }),
          crit({ criterion: "eligibility", rating: "unclear", label: "Eligibility" }),
        ]}
      />,
    );
    const byName = (n: string) => chips(container).find((el) => el.dataset.criterion === n)!;
    const trl = byName("trl");
    const elig = byName("eligibility");
    expect(trl).toHaveTextContent("Technology Readiness Level ?");
    expect(trl.dataset.tone).toBe("unsure");
    expect(elig).toHaveTextContent("Eligibility ?");
    expect(elig.dataset.tone).toBe("unsure");
  });

  it("shows an unrecognised rating's raw string, never a blank chip", () => {
    // `call_ranking_criteria.rating` is a bare String with no CHECK, so a model
    // that starts answering "probably" reaches this component unfiltered.
    const { container } = render(
      <CriterionChips criteria={[crit({ criterion: "eligibility", rating: "probably", label: "Eligibility" })]} />,
    );
    const [chip] = chips(container);
    expect(chip).toHaveTextContent("Eligibility probably");
    expect(chip.dataset.tone).toBe("unsure");
  });

  it("renders topic_fit out of five, and tones it at the 4 boundary", () => {
    // D6: >= 4 passes, everything below is unsure — `fail` is reserved for a
    // hard gate something actually failed.
    const { container, rerender } = render(
      <CriterionChips criteria={[crit({ criterion: "topic_fit", rating: "4", label: "Topic fit" })]} />,
    );
    expect(chips(container)[0]).toHaveTextContent("Topic fit 4/5");
    expect(chips(container)[0].dataset.tone).toBe("pass");
    rerender(<CriterionChips criteria={[crit({ criterion: "topic_fit", rating: "3", label: "Topic fit" })]} />);
    expect(chips(container)[0]).toHaveTextContent("Topic fit 3/5");
    expect(chips(container)[0].dataset.tone).toBe("unsure");
  });

  it("renders a criterion this build has never heard of, at the end of the row", () => {
    // The gate in tests/architecture/test_criterion_vocabulary.py exists to make
    // this state impossible to ship, but it must degrade, not blank out: the
    // label is server-computed and Title-cases an unknown key (T5-2).
    const { container } = render(
      <CriterionChips
        criteria={[
          crit({ criterion: "co_funding", rating: "pass", label: "Co Funding" }),
          crit({ criterion: "topic_fit", rating: "5", label: "Topic fit" }),
        ]}
      />,
    );
    expect(chips(container).map((el) => el.dataset.criterion)).toEqual(["topic_fit", "co_funding"]);
    expect(chips(container)[1]).toHaveTextContent("Co Funding ✓");
  });
});

describe("expansion", () => {
  const quoted = crit({
    criterion: "eligibility",
    rating: "pass",
    label: "Eligibility",
    rationale: "The consortium requirement is met by the two named partners.",
    quoted_evidence: "Applicants must be established in at least two member states.",
  });

  it("hides the quote and the reasoning until the chip is expanded", () => {
    render(<CriterionChips criteria={[quoted]} />);
    expect(screen.queryByText(/must be established/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Eligibility/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("shows the funder's quote FIRST and the model's reasoning after it, labelled", () => {
    render(<CriterionChips criteria={[quoted]} />);
    fireEvent.click(screen.getByRole("button", { name: /Eligibility/ }));
    const quote = screen.getByText(/must be established/);
    expect(quote.tagName).toBe("BLOCKQUOTE");
    // Tied to the stylesheet check below: this is the element that must carry
    // the CSS-modules `.quote` class, not just render as a <blockquote>.
    expect(quote.className).toContain("quote");
    const reasoning = screen.getByText(/consortium requirement/);
    // Never interleaved. The bitmask below is the ORDER assertion only, and is
    // not the sibling gate: DOCUMENT_POSITION_FOLLOWING is also set for
    // CONTAINED_BY, so nesting the <p> inside the <blockquote> satisfies it
    // (T5-9). The block structure is pinned directly instead: the reasoning is
    // not inside the quote, and the two share a parent.
    expect(quote.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(quote).not.toContainElement(reasoning);
    expect(reasoning.parentElement).toBe(quote.parentElement);
    expect(screen.getByText("The model's reasoning:")).toBeInTheDocument();
  });

  it("labels a reason-code rationale as a code, not as reasoning", () => {
    render(
      <CriterionChips
        criteria={[
          crit({ criterion: "budget", rating: "unknown", label: "Budget", rationale: "currency_mismatch" }),
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Budget/ }));
    expect(screen.getByText("Reason code:")).toBeInTheDocument();
    expect(screen.getByText("currency_mismatch").tagName).toBe("CODE");
    expect(screen.queryByText("The model's reasoning:")).not.toBeInTheDocument();
  });

  it("does not make a chip with nothing to show into a button", () => {
    const { container } = render(
      <CriterionChips criteria={[crit({ criterion: "budget", rating: "pass", label: "Budget" })]} />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(chips(container)[0].tagName).toBe("SPAN");
    expect(chips(container)[0]).toHaveTextContent("Budget ✓");
  });

  it("keeps at most one chip open", () => {
    const other = crit({ ...quoted, criterion: "exclusions", label: "Exclusions" });
    const { container } = render(<CriterionChips criteria={[quoted, other]} />);
    fireEvent.click(screen.getByRole("button", { name: /Eligibility/ }));
    fireEvent.click(screen.getByRole("button", { name: /Exclusions/ }));
    expect(screen.getByRole("button", { name: /Eligibility/ })).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByRole("button", { name: /Exclusions/ })).toHaveAttribute("aria-expanded", "true");
    expect(container.querySelectorAll("blockquote")).toHaveLength(1);
  });
});

it("sets the quote in Georgia, and uses Georgia for nothing else here", () => {
  // Spec §5: Georgia on quoted evidence is a design requirement, because it is
  // what makes the quote read as 'the funder said this'. jsdom applies no
  // stylesheet, so the rule is asserted against the module's source.
  // Two things that do NOT work here, both measured: `import ... ?raw` (vitest
  // runs with css:false and hands back the CSS-modules class proxy for a
  // *.module.css id whatever the suffix), and `new URL(..., import.meta.url)`
  // (under jsdom that base is an http: URL, which readFileSync refuses). The
  // vitest root is frontend/, so read from there.
  const css = readFileSync("src/components/CriterionChips.module.css", "utf8");
  const quoteRule = css.match(/\.quote\s*\{[^}]*\}/)?.[0] ?? "";
  expect(quoteRule).toMatch(/Georgia/);
  expect(css.match(/Georgia/g)).toHaveLength(1);
});

it("never wears the deadline band's colour on an unsure chip", () => {
  // theme.match.css annotates --app-warn as "the ≤30-day deadline band" (spec
  // §5); §4.2's "coloured by rating" governs pass/fail only. A `.unsure` rule
  // that reaches for --app-warn would make a gate-could-not-judge chip look
  // like a looming deadline, so the token must not appear anywhere in this
  // stylesheet at all.
  const css = readFileSync("src/components/CriterionChips.module.css", "utf8");
  expect(css).not.toMatch(/--app-warn/);
  // Positive control: pass/fail must still be coloured, so this test cannot
  // pass merely because the file was emptied out.
  expect(css).toMatch(/\.pass \.value\s*\{[^}]*--app-good/);
  expect(css).toMatch(/\.fail \.value\s*\{[^}]*--app-dim/);
});
