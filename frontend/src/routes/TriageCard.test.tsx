import { readFileSync } from "node:fs";
import { join } from "node:path";

import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CallItem } from "../api/dashboard";
import type { components } from "../api/schema.gen";
import { renderWithProviders } from "../test-utils";
import { TriageCard } from "./TriageCard";

type Criterion = components["schemas"]["CallListCriterionOut"];

// The API's own CallOut shape, with every optional field at its COMMON value —
// absent. Spec §5: CallRanking.summary and applicant_notes are written only by
// the remote path, and that path is capped at RANKING_REMOTE_DAILY_CAP, so the
// rich card is the minority and the bare one is what most calls look like.
function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 1,
    title: "Precision oncology in clinical practice",
    deadline: null,
    deadline_local_date: null,
    published_at: null,
    source_name: "Vinnova",
    source_slug: "vinnova",
    aggregate_score: null,
    programme: null,
    budget_min: null,
    budget_max: null,
    currency: null,
    url: "https://example.org/calls/1",
    summary: null,
    keywords: [],
    criteria: [],
    applicant_note_count: 0,
    escalated: false,
    ...over,
  };
}

function crit(over: Partial<Criterion> & { criterion: string; rating: string }): Criterion {
  return {
    label: over.criterion,
    is_topic_fit: over.criterion === "topic_fit",
    is_reason_code: false,
    rationale: null,
    quoted_evidence: null,
    ...over,
  };
}

// A local date N days from now, in Europe/Stockholm — the same clock
// deadlineLocalView reads, so "12 days left" is 12 here too.
function localDateIn(days: number): string {
  const today = new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Stockholm" });
  return new Date(new Date(today + "T00:00:00Z").getTime() + days * 86_400_000)
    .toISOString()
    .slice(0, 10);
}

function fields(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>("[data-field]")].map(
    (el) => el.dataset.field ?? "",
  );
}

describe("gate 1 — what a card shows", () => {
  it("shows title, funder and deadline", () => {
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          source_name: "Cancerfonden",
          deadline: "2027-03-01T12:00:00Z",
          deadline_local_date: "2027-03-01",
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent(
      "Precision oncology in clinical practice",
    );
    expect(container.querySelector('[data-field="funder"]')).toHaveTextContent("Cancerfonden");
    expect(container.querySelector('[data-field="deadline"]')).toHaveTextContent("2027-03-01");
    // The feed's j/k move REAL focus onto this element and address it by id.
    expect(container.querySelector("article")).toHaveAttribute("data-call-id", "1");
    expect(container.querySelector("article")).toHaveAttribute("tabindex", "-1");
  });

  it("orders the fields the way §5's table does: what is this, what does it fund, why is it here", () => {
    // Order is the design decision, not a by-product: a card that leads with the
    // chips answers "why is it in front of me" before "what is this".
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          programme: "Utmaningsdriven innovation",
          deadline: "2027-03-01T12:00:00Z",
          deadline_local_date: localDateIn(12),
          budget_min: 100_000,
          budget_max: 250_000,
          currency: "SEK",
          summary: "Supports clinical implementation studies.",
          keywords: ["oncology"],
          criteria: [
            crit({ criterion: "topic_fit", rating: "5", quoted_evidence: "clinical genomics" }),
          ],
          escalated: true,
          applicant_note_count: 3,
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(fields(container)).toEqual([
      "funder",
      "programme",
      "deadline",
      "days-left",
      "budget",
      "summary",
      "matched-sentence",
      "keywords",
      "things-to-check",
    ]);
  });
});

describe("gate 2 — the common case: every optional field absent", () => {
  it("renders, and says so instead of collapsing the summary section", () => {
    // B27 killer. summary null, keywords [], criteria [], programme null, every
    // budget field null — the SHAPE most calls arrive in.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ deadline: "2027-03-01T12:00:00Z", deadline_local_date: "2027-03-01" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toBeInTheDocument();
    const noSummary = container.querySelector('[data-field="no-summary"]');
    expect(noSummary).not.toBeNull();
    // The ACTUAL copy, not a testid: §5 asks for a sentence that reads as a fact
    // about the source, and a gate that only checked the attribute would pass
    // over an empty <p data-field="no-summary" />.
    expect(noSummary).toHaveTextContent("This source publishes no description with the call.");
    // ...and a link OUT, which is the other half of §5's requirement.
    expect(screen.getByRole("link", { name: /read it at the funder/i })).toHaveAttribute(
      "href",
      "https://example.org/calls/1",
    );
    // Nothing empty is left behind: no chips row, no keyword row, no programme
    // separator dangling after the funder.
    expect(container.querySelector("[data-criterion]")).toBeNull();
    expect(container.querySelector('[data-field="keywords"]')).toBeNull();
    expect(container.querySelector('[data-field="programme"]')).toBeNull();
    expect(container.querySelector('[data-field="funder"]')?.parentElement?.textContent).toBe(
      "Vinnova",
    );
  });

  it("prints the summary itself when the source did publish one", () => {
    // The other side of B27: dropping the null branch must not be the only thing
    // this file can tell apart from dropping BOTH branches.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ summary: "Supports clinical implementation studies." })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="summary"]')).toHaveTextContent(
      "Supports clinical implementation studies.",
    );
    expect(container.querySelector('[data-field="no-summary"]')).toBeNull();
  });
});

describe("gate 12 — the URL is the state, and the card's link preserves it (D11)", () => {
  it("links the title to /calls/{id}?profile={p}&view={v} — BOTH parameters", () => {
    // B31 killer. The detail page reads ?profile= for the ranking it shows and
    // ?view= for where its back link returns to; dropping either strands the
    // reader on a page that cannot get them back where they were.
    renderWithProviders(
      <TriageCard call={call({ id: 1 })} expanded profileId={7} view="hidden" />,
    );
    expect(screen.getByRole("link", { name: /precision oncology/i })).toHaveAttribute(
      "href",
      "/calls/1?profile=7&view=hidden",
    );
  });

  it("carries the view alone when no profile is resolved", () => {
    renderWithProviders(
      <TriageCard call={call({ id: 42 })} expanded profileId={null} view="inbox" />,
    );
    expect(screen.getByRole("link", { name: /precision oncology/i })).toHaveAttribute(
      "href",
      "/calls/42?view=inbox",
    );
  });
});

describe("gate 9 (button half) — Return to inbox (D2)", () => {
  it("offers the un-decide button in Saved and Hidden, and calls back with the call", () => {
    // Keys are undiscoverable, and the spec's own scenario ("un-hide returns it
    // to the inbox") was unperformable without a control. The button issues the
    // SAME request `u` does — TriageFeed.test.tsx pins which request that is.
    const onUndecide = vi.fn();
    renderWithProviders(
      <TriageCard
        call={call({ id: 5 })}
        expanded
        profileId={7}
        view="hidden"
        onUndecide={onUndecide}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Return to inbox" }));
    expect(onUndecide.mock.calls).toHaveLength(1);
    expect((onUndecide.mock.calls[0] as [CallItem])[0].id).toBe(5);
  });

  it("does NOT offer it in the Inbox, where there is no decision to undo", () => {
    renderWithProviders(
      <TriageCard
        call={call({ id: 5 })}
        expanded
        profileId={7}
        view="inbox"
        onUndecide={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Return to inbox" })).toBeNull();
  });

  it("does not offer it on a collapsed card either", () => {
    renderWithProviders(
      <TriageCard
        call={call({ id: 5 })}
        expanded={false}
        profileId={7}
        view="saved"
        onUndecide={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Return to inbox" })).toBeNull();
  });
});

describe("the fields that are conditional", () => {
  it("omits the separator with the programme, and prints both when there is one", () => {
    const { container: without } = renderWithProviders(
      <TriageCard call={call({ source_name: "Vinnova" })} expanded profileId={7} view="inbox" />,
    );
    expect(without.querySelector('[data-field="funder"]')?.parentElement?.textContent).toBe(
      "Vinnova",
    );
    const { container: with_ } = renderWithProviders(
      <TriageCard
        call={call({ source_name: "Vinnova", programme: "UDI" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(with_.querySelector('[data-field="funder"]')?.parentElement?.textContent).toBe(
      "Vinnova · UDI",
    );
  });

  it("shows a budget RANGE when min differs from max, and one figure when they agree", () => {
    const { container: range } = renderWithProviders(
      <TriageCard
        call={call({ budget_min: 100_000, budget_max: 250_000, currency: "SEK" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(range.querySelector('[data-field="budget"]')).toHaveTextContent("100,000 – 250,000 SEK");
    const { container: flat } = renderWithProviders(
      <TriageCard
        call={call({ budget_min: 250_000, budget_max: 250_000, currency: "SEK" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(flat.querySelector('[data-field="budget"]')).toHaveTextContent("250,000 SEK");
    expect(flat.querySelector('[data-field="budget"]')?.textContent).not.toContain("–");
  });

  it("counts the days left beside the deadline, and prints none for a closed call", () => {
    const { container: open } = renderWithProviders(
      <TriageCard
        call={call({ deadline: "2099-01-01T00:00:00Z", deadline_local_date: localDateIn(12) })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(open.querySelector('[data-field="days-left"]')).toHaveTextContent("12 days left");
    const { container: closed } = renderWithProviders(
      <TriageCard
        call={call({ deadline: "2020-01-01T00:00:00Z", deadline_local_date: localDateIn(-9) })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(closed.querySelector('[data-field="days-left"]')).toBeNull();
    expect(closed.querySelector('[data-field="deadline"]')).toHaveTextContent(/^Closed ·/);
  });

  it("offers 'N things to check' only for an escalated call that actually has notes", () => {
    const both = renderWithProviders(
      <TriageCard
        call={call({ escalated: true, applicant_note_count: 3 })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(both.container.querySelector('[data-field="things-to-check"]')).toHaveTextContent(
      "3 things to check",
    );
    const noNotes = renderWithProviders(
      <TriageCard
        call={call({ escalated: true, applicant_note_count: 0 })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(noNotes.container.querySelector('[data-field="things-to-check"]')).toBeNull();
    const notEscalated = renderWithProviders(
      <TriageCard
        call={call({ escalated: false, applicant_note_count: 3 })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(notEscalated.container.querySelector('[data-field="things-to-check"]')).toBeNull();
  });

  it("collapses to title, funder and days left when it is not the focused card", () => {
    // §5: "the rest are collapsed to title, funder and days left". The evidence
    // belongs to the ONE expanded card; a feed of fully-expanded cards is the
    // firehose again.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          summary: "Supports clinical implementation studies.",
          keywords: ["oncology"],
          criteria: [
            crit({ criterion: "topic_fit", rating: "5", quoted_evidence: "clinical genomics" }),
          ],
        })}
        expanded={false}
        profileId={7}
        view="inbox"
      />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toBeInTheDocument();
    expect(container.querySelector('[data-field="funder"]')).not.toBeNull();
    expect(container.querySelector('[data-field="summary"]')).toBeNull();
    expect(container.querySelector("[data-criterion]")).toBeNull();
    expect(container.querySelector('[data-field="matched-sentence"]')).toBeNull();
  });

  it("gives the matched sentence prime space, in the funder's own words", () => {
    // §5: "The evidence is visible, not behind a click." topic_fit's quote wins
    // over another criterion's, because it is the one that answers "why is this
    // in front of me".
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          criteria: [
            crit({
              criterion: "eligibility",
              rating: "pass",
              quoted_evidence: "open to Swedish universities",
            }),
            crit({
              criterion: "topic_fit",
              rating: "5",
              quoted_evidence: "clinical genomics and molecular tumour boards",
            }),
          ],
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="matched-sentence"]')).toHaveTextContent(
      "clinical genomics and molecular tumour boards",
    );
  });
});

describe("gate 8 — no raw score on the card", () => {
  // Spec §4.2 keeps the BAND for sorting and the digest and shows the NUMBER
  // nowhere. Task 6 removed it from the detail page; this is the same guarantee
  // on the one screen §4.2 is actually about.
  //
  // TWO assertions, because each one alone has a hole big enough to drive B25b
  // through:
  //
  //   - the RENDER check can only prove that THIS fixture's score string is
  //     absent. It passes for free on a fixture whose aggregate_score is null,
  //     and it would false-POSITIVE if the score collided with a legitimate
  //     number on the card. Hence: score 87, deadline 12 days out, budget
  //     100,000–250,000, topic_fit 5/5, 3 notes — 87 appears nowhere by accident.
  //   - the SOURCE check cannot be dodged by a fixture at all, but it is a string
  //     scan: `call["aggregate" + "_score"]` walks straight past it (B25c). It is
  //     a smoke; (c) is the gate.
  //
  // Together they cover both mistakes. Neither subsumes the other.
  const scored = call({
    aggregate_score: 87,
    deadline: "2099-01-01T00:00:00Z",
    deadline_local_date: localDateIn(12),
    budget_min: 100_000,
    budget_max: 250_000,
    currency: "SEK",
    summary: "Supports clinical implementation studies.",
    keywords: ["oncology"],
    escalated: true,
    applicant_note_count: 3,
    criteria: [
      crit({
        criterion: "topic_fit",
        rating: "5",
        label: "Topic fit",
        quoted_evidence: "clinical genomics",
      }),
      crit({ criterion: "eligibility", rating: "pass", label: "Eligibility" }),
    ],
  });

  it("renders the criterion chips instead — and no band, no badge, no raw number", () => {
    const { container } = renderWithProviders(
      <TriageCard call={scored} expanded profileId={7} view="inbox" />,
    );
    // The card is NOT vacuously clean: the reason-shaped answer is present.
    expect([...container.querySelectorAll("[data-criterion]")].map((el) => el.textContent)).toEqual(
      ["Topic fit 5/5", "Eligibility ✓"],
    );
    // MatchBadge's own hook — the full edition's pill sets data-band.
    expect(container.querySelector("[data-band]")).toBeNull();
    // ...and the number itself is nowhere in the CARD's own text.
    //
    // Scoped to the <article>, not to `container`: MantineProvider injects ~8 kB
    // of CSS custom properties into a <style> inside the render container, and
    // its text contains "87" (rgba components, the 87.99375em breakpoint, ...).
    // A container-wide `not.toContain("87")` therefore fails on CORRECT code —
    // measured on this branch. Any "no bare number" gate written against the
    // whole container is a false positive waiting to happen (T7-2).
    const card = container.querySelector("article");
    expect(card).not.toBeNull();
    expect(card?.textContent).not.toContain("87");
  });

  it("does not name aggregate_score in its source at all", () => {
    const src = readFileSync(join(import.meta.dirname, "TriageCard.tsx"), "utf8");
    // Comments say the word on purpose (they explain WHY it is absent), so the
    // scan is over the code with comment lines stripped.
    const code = src
      .split("\n")
      .filter((l) => !l.trim().startsWith("//") && !l.trim().startsWith("*"))
      .join("\n");
    expect(code).not.toContain("aggregate_score");
  });
});
