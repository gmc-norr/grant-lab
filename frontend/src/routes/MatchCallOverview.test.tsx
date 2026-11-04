// frontend/src/routes/MatchCallOverview.test.tsx
import { MantineProvider } from "@mantine/core";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import type { CallDetail } from "../api/calls";
import { theme } from "../theme/theme";
import { MatchCallOverview } from "./MatchCallOverview";

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
    // Added 2026-10-26: the common case is a pair that ranks fine, so the
    // "couldn't rank" line renders in NO test but its own two below.
    ranking_failed: false, ranking_failed_at: null,
  },
  criteria: [],
  best_profile_id: 7,
} as unknown as CallDetail;

function renderBody(data: CallDetail) {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter>{<MatchCallOverview {...data} />}</MemoryRouter>
    </MantineProvider>,
  );
}

it("the MATCH edition renders NO Start-drafting control — /cockpit/new is not mounted", () => {
  // It is a <Button component={Link}>, so it can carry a link or a button role
  // depending on the react-router version; both are checked, and so is every
  // rendered href — a renamed link would pass a name-only assertion.
  renderBody(DATA);
  expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /start drafting/i })).not.toBeInTheDocument();
  for (const a of screen.queryAllByRole("link")) {
    expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
  }
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

it("the MATCH edition keeps the reason badge — it names a reason, not a number", () => {
  // Spec §4.2 strips the raw aggregate, NOT the reason for it: "Budget" is the
  // criterion that drove the score and is exactly what a researcher is owed
  // instead of the number. This is the assertion that has to survive the match
  // gate, so a `!isMatchEdition &&` on that Badge must redden it.
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

// GATE 4 IS AN OR OF THREE, NOT ANY ONE OF THEM. BARE above proves the
// all-three-empty case; the two REASONED/ESCALATED fixtures above give the
// disjunction TWO truthy terms at once (both inherit `rationale: "Strong
// fit."` and `embed_similarity: 0.42` from DATA), so a mutant that drops any
// single term from `rankingCardHasContent` — `|| Boolean(ranking.rationale)`,
// say — still finds a second truthy term in every existing match-edition
// fixture and stays green. Each fixture below starts from BARE (all three
// empty) and restores exactly ONE term, so each one is a separate, minimal
// witness for its own disjunct: only the mutant that drops THAT term can
// leave THAT fixture's card ungated.
const REASON_LABEL_ONLY = {
  ...BARE,
  ranking: { ...BARE.ranking, aggregate_reason_label: "Budget" },
} as unknown as CallDetail;

const RATIONALE_ONLY = {
  ...BARE,
  ranking: { ...BARE.ranking, rationale: "Strong fit." },
} as unknown as CallDetail;

const EMBED_SIMILARITY_ONLY = {
  ...BARE,
  ranking: { ...BARE.ranking, embed_similarity: 0.42 },
} as unknown as CallDetail;

it("the MATCH edition renders the Ranking card when only the reason label carries content", () => {
  renderBody(REASON_LABEL_ONLY);
  expect(screen.getByRole("region", { name: "Ranking" })).toBeInTheDocument();
  expect(reasonBadge()).toBeInTheDocument();
});

it("the MATCH edition renders the Ranking card when only the rationale carries content", () => {
  renderBody(RATIONALE_ONLY);
  expect(screen.getByRole("region", { name: "Ranking" })).toBeInTheDocument();
  expect(
    within(screen.getByRole("region", { name: "Ranking" })).getByText("Strong fit."),
  ).toBeInTheDocument();
});

it("the MATCH edition renders the Ranking card when only the cosine similarity carries content", () => {
  renderBody(EMBED_SIMILARITY_ONLY);
  expect(screen.getByRole("region", { name: "Ranking" })).toBeInTheDocument();
  expect(
    within(screen.getByRole("region", { name: "Ranking" })).getByText("Cosine similarity: 0.420"),
  ).toBeInTheDocument();
});

// --- "couldn't rank" on the DETAIL page, in BOTH editions -------------------
//
// The card (TriageCard) is the primary surface for this line and has its own
// gates; the detail page matters because the FULL edition prints the raw
// aggregate at 2.5rem, which is the single most authoritative-looking stale
// number in the product. The two editions render through DIFFERENT components
// (CallDetailBody vs MatchCallOverview), so one test each — a line added to only
// one of them is exactly the drift this file exists to catch.
const RANK_FAILED = {
  ...DATA,
  ranking: {
    ...DATA.ranking,
    ranking_failed: true,
    ranking_failed_at: "2026-10-25T03:17:42Z",
  },
} as CallDetail;

it("says it when the re-rank keeps failing", () => {
  const { container } = renderBody(RANK_FAILED);
  expect(container.querySelector("[data-band]")).toBeNull();
  expect(container.querySelector('[data-field="ranking-failed"]')).toHaveTextContent(
    "Couldn't rank this call with the local model (last tried 2026-10-25)",
  );
});

it("says nothing for a ranking that is current", () => {
  const { container } = renderBody(DATA);
  expect(container.querySelector('[data-field="ranking-failed"]')).toBeNull();
});
