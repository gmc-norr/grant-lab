// frontend/src/routes/CallDetailBody.test.tsx
import { MantineProvider } from "@mantine/core";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { CallDetail } from "../api/calls";
import { theme } from "../theme/theme";
import { CallDetailBody } from "./CallDetailBody";

// The Start-drafting button is edition-gated and isMatchEdition is a BUILD-TIME
// constant, so a test that read the real value would pass or fail by environment.
// Mutable holder + GETTER, the pattern from AdminNav.test.tsx:25-34: the getter is
// load-bearing, because a plain value would be frozen at import time and the match
// case below would silently run the FULL branch.
const edition = vi.hoisted(() => ({ isMatch: false }));
vi.mock("../edition", () => ({
  get isMatchEdition() {
    return edition.isMatch;
  },
  get APP_EDITION() {
    return edition.isMatch ? "match" : "full";
  },
  resolveEdition: (raw: string | undefined) => (raw === "match" ? "match" : "full"),
}));

afterEach(() => {
  edition.isMatch = false;
});

const DATA = {
  call: {
    id: 5, title: "Genomics Grant", source_name: "Vinnova", programme: "Prog",
    external_id: "EX-1", published_at: null, deadline: null, budget_min: null,
    budget_max: null, currency: null, url: "https://x", keywords: ["dna"], summary: "Sum.",
  },
  ranking: {
    aggregate_score: 72, aggregate_reason_label: null, model_label: "Sonnet 4.6",
    rationale: "Strong fit.", embed_similarity: 0.42, escalated: false,
    escalated_from_score: null, analysis_model_label: "Sonnet 4.6", summary: "Deep summary.",
    applicant_notes: [{ category: "budget", heading: "Budget & funding", text: "Note." }],
  },
  criteria: [],
  best_profile_id: 7,
} as unknown as CallDetail;

function renderBody(data: CallDetail) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{<CallDetailBody {...data} />}</MemoryRouter>
    </MantineProvider>,
  );
}

it("renders the title heading, ranking score band, and analyst notes", () => {
  renderBody(DATA);
  expect(screen.getByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByText("72")).toHaveAttribute("data-band", "strong");
  expect(screen.getByText("Deep summary.")).toBeInTheDocument();
  expect(screen.getByText("Budget & funding")).toBeInTheDocument();
});

it("the FULL edition links Start drafting to /cockpit/new with the call + best profile", () => {
  // The control for the match test below. Without it a fix that dropped the
  // button unconditionally — i.e. broke the full product, which is run-record
  // ESCALATE E1 — would leave the match test green.
  renderBody(DATA);
  const link = screen.getByRole("link", { name: /start drafting/i });
  const href = link.getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=5");
  expect(href).toContain("profile_id=7");
});

it("the FULL edition omits profile_id when there is no best profile", () => {
  renderBody({ ...DATA, best_profile_id: null } as CallDetail);
  const href = screen.getByRole("link", { name: /start drafting/i }).getAttribute("href") ?? "";
  expect(href).toContain("call_id=5");
  expect(href).not.toContain("profile_id");
});

it("the MATCH edition renders NO Start-drafting control — /cockpit/new is not mounted", () => {
  // It is a <Button component={Link}>, so it can carry a link or a button role
  // depending on the react-router version; both are checked, and so is every
  // rendered href — a renamed link would pass a name-only assertion.
  edition.isMatch = true;
  renderBody(DATA);
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.queryAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
});

it("renders the low band for a zero aggregate score", () => {
  renderBody({ ...DATA, ranking: { ...DATA.ranking, aggregate_score: 0 } } as CallDetail);
  expect(screen.getByText("0")).toHaveAttribute("data-band", "low");
});

// THE REASON BADGE. Until this fixture existed, no fixture in this file gave
// `aggregate_reason_label` a truthy value, so the badge branch rendered in NO
// test at all — and the comment in CallDetailBody.tsx listing it among the things
// that are NOT gated was an unchecked claim (T6-5). "Budget" is a real value:
// web/filters.py maps the `budget` criterion code to it.
//
// NOTE the interaction this fixture also documents: a truthy label SUPPRESSES the
// rationale (`ranking.rationale && !ranking.aggregate_reason_label`), so the two
// are alternatives, not siblings — which is why this is its own fixture rather
// than a field added to ESCALATED, whose match test asserts the rationale is on
// screen.
const REASONED = {
  ...DATA,
  ranking: { ...DATA.ranking, aggregate_reason_label: "Budget" },
} as CallDetail;

// SCOPED TO THE RANKING SECTION, and it has to be: the Details section renders a
// "Budget" field label of its own (`tt="uppercase"` is CSS, so its DOM text is the
// same string), and an unscoped getByText("Budget") fails with "found multiple
// elements" — measured, not assumed.
function reasonBadge() {
  return within(screen.getByRole("region", { name: "Ranking" })).queryByText("Budget");
}

it("the FULL edition names the aggregate's reason in a badge", () => {
  // The control: a badge that disappeared from both editions would leave the
  // match assertion below green.
  renderBody(REASONED);
  expect(reasonBadge()).toBeInTheDocument();
});

it("the MATCH edition keeps the reason badge — it names a reason, not a number", () => {
  // Spec §4.2 strips the raw aggregate, NOT the reason for it: "Budget" is the
  // criterion that drove the score and is exactly what a researcher is owed
  // instead of the number. This is the assertion that has to survive the match
  // gate, so a `!isMatchEdition &&` on that Badge must redden it.
  edition.isMatch = true;
  const { container } = renderBody(REASONED);
  expect(reasonBadge()).toBeInTheDocument();
  // And it really is the match edition — otherwise this is the full-edition test
  // twice over.
  expect(container.querySelector("[data-band]")).toBeNull();
});

// Spec §4.2: "The band (high / medium / low) is retained for sorting and for the
// digest, but the raw aggregate is not shown in the UI." THREE things in this card
// print a raw aggregate, not one: the figure, its "Aggregate score" heading, and
// the escalation note — `escalated_from_score` IS an aggregate score, the local
// one from before the remote re-rank (ranking/rank_pair.py sets it from
// `local_agg.score`, and ranking/golden_set.py subtracts it from `aggregate_score`
// as the same unit). All three are edition-gated. What is NOT gated: the reason
// badge, the model attribution, the rationale and the cosine line — they name a
// reason rather than a number, and the cosine value reaches the match edition
// through the criterion breakdown anyway (web/call_detail_view.py writes it into a
// criterion `detail`, and §4.2 keeps the breakdown).
const ESCALATED = {
  ...DATA,
  ranking: { ...DATA.ranking, escalated: true, escalated_from_score: 55 },
} as CallDetail;

it("the FULL edition keeps the aggregate figure, its heading and the escalation note", () => {
  // The control for the match test below, and the only thing standing between
  // Global Constraint 3 and a gate that fired in BOTH editions: a change that
  // stripped the score from the full product would leave the match test green.
  const { container } = renderBody(ESCALATED);
  expect(screen.getByText("Aggregate score")).toBeInTheDocument();
  expect(container.querySelector("[data-band]")).toHaveTextContent("72");
  expect(screen.getByText(/Escalated from 55/)).toBeInTheDocument();
});

it("the MATCH edition shows no raw aggregate, and still renders the rest of the ranking card", () => {
  // What would TRIVIALLY pass the four absence assertions: a component that threw
  // and rendered nothing, or a `{ranking && …}` section gated away wholesale — the
  // number is "absent" either way. Both are excluded by the two PRESENCE
  // assertions at the end, which deliberately name contents of the SAME ranking
  // card rather than any old string from the page: a proof-of-render taken from
  // OUTSIDE this section (the page title, "Details") still passes with the whole
  // card gone — measured, that exact wording survived mutation B22b (T6-2).
  // `[data-band]` is queried structurally so the figure is caught even if its
  // value changes, and the digits are checked against the whole rendered text so a
  // re-worded label cannot smuggle the number back in.
  edition.isMatch = true;
  const { container } = renderBody(ESCALATED);
  expect(screen.queryByText("Aggregate score")).not.toBeInTheDocument();
  expect(container.querySelector("[data-band]")).toBeNull();
  expect(screen.queryByText(/Escalated from/)).not.toBeInTheDocument();
  expect(container.textContent ?? "").not.toMatch(/\b(72|55)\b/);
  expect(screen.getByText("Analyzed by Sonnet 4.6")).toBeInTheDocument();
  expect(screen.getByText("Strong fit.")).toBeInTheDocument();
});

// GATE 4 — the empty bordered card. Once the three raw-aggregate guards have
// fired, a match-edition ranking card can hold nothing a researcher can act on:
// no reason label, no rationale, no cosine. That state is reachable — the wire
// type is `rationale: str` (not `str | None`, so "" is a real value),
// `aggregate_reason_label: str | None` is null in every fixture in this repo, and
// `embed_similarity: float | None` is null whenever a vector is missing
// ("No cosine — one or both vectors missing, gate UNKNOWN." in
// web/call_detail_view.py). What is left is the model-attribution badge, which is
// chrome, not a match reason: a bordered, tinted card whose entire content is
// "Analyzed by X" is F-11's empty "Ranking" card.
const BARE = {
  ...DATA,
  ranking: {
    ...DATA.ranking,
    aggregate_reason_label: null,
    rationale: "",
    embed_similarity: null,
    summary: null,
    applicant_notes: [],
  },
} as unknown as CallDetail;

it("the MATCH edition renders no Ranking card at all when the guards empty it", () => {
  edition.isMatch = true;
  renderBody(BARE);
  expect(screen.queryByRole("region", { name: "Ranking" })).not.toBeInTheDocument();
  // Not merely "the card is empty" — the card, its border and its heading are
  // gone. The attribution badge is the one thing that would otherwise still be
  // inside it, so its absence is what distinguishes gate 4 from gate 1–3.
  expect(screen.queryByText(/^Analyzed by/)).not.toBeInTheDocument();
  // Proof of render: this is the gate firing, not a component that threw.
  expect(screen.getByRole("heading", { name: "Genomics Grant" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Call details" })).toBeInTheDocument();
});

it("the FULL edition still renders the Ranking card for that same bare ranking", () => {
  // The control: gate 4 must not reach the full edition, where the figure and its
  // heading are always inside the card and the card is therefore never empty.
  renderBody(BARE);
  expect(screen.getByRole("region", { name: "Ranking" })).toBeInTheDocument();
  expect(screen.getByText("Aggregate score")).toBeInTheDocument();
  expect(screen.getByText("Analyzed by Sonnet 4.6")).toBeInTheDocument();
});
