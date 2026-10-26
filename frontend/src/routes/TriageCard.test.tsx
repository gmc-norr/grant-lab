import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { CallItem } from "../api/dashboard";
import type { components } from "../api/schema.gen";
import { renderWithProviders } from "../test-utils";
import { TriageCard } from "./TriageCard";

afterEach(() => {
  vi.restoreAllMocks();
});

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
    decision_note: null,
    ranking_failed: false,
    ranking_failed_at: null,
    ...over,
  };
}

function crit(
  over: Partial<Criterion> & { criterion: string; rating: string },
): Criterion {
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
  const today = new Date().toLocaleDateString("sv-SE", {
    timeZone: "Europe/Stockholm",
  });
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
    expect(container.querySelector('[data-field="funder"]')).toHaveTextContent(
      "Cancerfonden",
    );
    expect(
      container.querySelector('[data-field="deadline"]'),
    ).toHaveTextContent("2027-03-01");
    // The feed's j/k move REAL focus onto this element and address it by id.
    expect(container.querySelector("article")).toHaveAttribute(
      "data-call-id",
      "1",
    );
    expect(container.querySelector("article")).toHaveAttribute(
      "tabindex",
      "-1",
    );
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
            crit({
              criterion: "topic_fit",
              rating: "5",
              quoted_evidence: "clinical genomics",
            }),
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
        call={call({
          deadline: "2027-03-01T12:00:00Z",
          deadline_local_date: "2027-03-01",
        })}
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
    expect(noSummary).toHaveTextContent(
      "This source publishes no description with the call.",
    );
    // ...and a link OUT, which is the other half of §5's requirement.
    expect(
      screen.getByRole("link", { name: /read it at the funder/i }),
    ).toHaveAttribute("href", "https://example.org/calls/1");
    // Nothing empty is left behind: no chips row, no keyword row, no programme
    // separator dangling after the funder.
    expect(container.querySelector("[data-criterion]")).toBeNull();
    expect(container.querySelector('[data-field="keywords"]')).toBeNull();
    expect(container.querySelector('[data-field="programme"]')).toBeNull();
    expect(
      container.querySelector('[data-field="funder"]')?.parentElement
        ?.textContent,
    ).toBe("Vinnova");
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
      <TriageCard
        call={call({ id: 1 })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    expect(
      screen.getByRole("link", { name: /precision oncology/i }),
    ).toHaveAttribute("href", "/calls/1?profile=7&view=hidden");
  });

  it("carries the view alone when no profile is resolved", () => {
    renderWithProviders(
      <TriageCard
        call={call({ id: 42 })}
        expanded
        profileId={null}
        view="inbox"
      />,
    );
    expect(
      screen.getByRole("link", { name: /precision oncology/i }),
    ).toHaveAttribute("href", "/calls/42?view=inbox");
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
    expect(
      screen.queryByRole("button", { name: "Return to inbox" }),
    ).toBeNull();
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
    expect(
      screen.queryByRole("button", { name: "Return to inbox" }),
    ).toBeNull();
  });
});

describe("the fields that are conditional", () => {
  it("omits the separator with the programme, and prints both when there is one", () => {
    const { container: without } = renderWithProviders(
      <TriageCard
        call={call({ source_name: "Vinnova" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(
      without.querySelector('[data-field="funder"]')?.parentElement
        ?.textContent,
    ).toBe("Vinnova");
    const { container: with_ } = renderWithProviders(
      <TriageCard
        call={call({ source_name: "Vinnova", programme: "UDI" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(
      with_.querySelector('[data-field="funder"]')?.parentElement?.textContent,
    ).toBe("Vinnova · UDI");
  });

  it("shows a budget RANGE when min differs from max, and one figure when they agree", () => {
    const { container: range } = renderWithProviders(
      <TriageCard
        call={call({
          budget_min: 100_000,
          budget_max: 250_000,
          currency: "SEK",
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(range.querySelector('[data-field="budget"]')).toHaveTextContent(
      "100,000 – 250,000 SEK",
    );
    const { container: flat } = renderWithProviders(
      <TriageCard
        call={call({
          budget_min: 250_000,
          budget_max: 250_000,
          currency: "SEK",
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(flat.querySelector('[data-field="budget"]')).toHaveTextContent(
      "250,000 SEK",
    );
    expect(
      flat.querySelector('[data-field="budget"]')?.textContent,
    ).not.toContain("–");
  });

  it("counts the days left beside the deadline, and prints none for a closed call", () => {
    const { container: open } = renderWithProviders(
      <TriageCard
        call={call({
          deadline: "2099-01-01T00:00:00Z",
          deadline_local_date: localDateIn(12),
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(open.querySelector('[data-field="days-left"]')).toHaveTextContent(
      "12 days left",
    );
    const { container: closed } = renderWithProviders(
      <TriageCard
        call={call({
          deadline: "2020-01-01T00:00:00Z",
          deadline_local_date: localDateIn(-9),
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(closed.querySelector('[data-field="days-left"]')).toBeNull();
    expect(closed.querySelector('[data-field="deadline"]')).toHaveTextContent(
      /^Closed ·/,
    );
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
    expect(
      both.container.querySelector('[data-field="things-to-check"]'),
    ).toHaveTextContent("3 things to check");
    const noNotes = renderWithProviders(
      <TriageCard
        call={call({ escalated: true, applicant_note_count: 0 })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(
      noNotes.container.querySelector('[data-field="things-to-check"]'),
    ).toBeNull();
    const notEscalated = renderWithProviders(
      <TriageCard
        call={call({ escalated: false, applicant_note_count: 3 })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(
      notEscalated.container.querySelector('[data-field="things-to-check"]'),
    ).toBeNull();
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
            crit({
              criterion: "topic_fit",
              rating: "5",
              quoted_evidence: "clinical genomics",
            }),
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
    expect(
      container.querySelector('[data-field="matched-sentence"]'),
    ).toBeNull();
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
    expect(
      container.querySelector('[data-field="matched-sentence"]'),
    ).toHaveTextContent("clinical genomics and molecular tumour boards");
  });
});

describe("gate 8 — the raw score IS on the card", () => {
  // REVERSED 2026-09-08. Spec §4.2 originally kept the band for sorting and the
  // digest and showed the number nowhere, and this gate asserted the card
  // neither rendered nor named `aggregate_score`. The customer's objection was
  // the one the spec never answered: the criterion chips appear only on the ONE
  // expanded card, so the other rows carried no match signal at all. Sort order
  // was the only ranking cue and nothing on screen said so.
  //
  // What did NOT change, and is still asserted here:
  //   - the chips are still there. Showing a number must not cost the reasons;
  //     that trade is what §4.2 was right about.
  //   - no band, no colour. §5 reserves colour for deadline urgency, so
  //     [data-band] — MatchBadge's own hook — must stay absent.
  //
  // The "87" scoping note from the old gate still applies in reverse: assertions
  // are scoped to [data-field="score"], never to `container`, because
  // MantineProvider injects ~8 kB of CSS whose text contains "87" (rgba
  // components, the 87.99375em breakpoint). A container-wide search for the
  // number would pass on a card that renders nothing at all.
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

  it("shows the score, with its scale, and still shows the criterion chips", () => {
    const { container } = renderWithProviders(
      <TriageCard call={scored} expanded profileId={7} view="inbox" />,
    );
    expect(container.querySelector('[data-field="score"]')?.textContent).toBe(
      "87",
    );
    // The denominator: a bare 87 beside a "5/5" chip reads as a different scale.
    expect(container.querySelector("article")?.textContent).toContain(
      "/ 100 match",
    );
    // Not traded for the reasons — both, or this change made the card worse.
    expect(
      [...container.querySelectorAll("[data-criterion]")].map(
        (el) => el.textContent,
      ),
    ).toEqual(["Topic fit 5/5", "Eligibility ✓"]);
  });

  it("shows it on a COLLAPSED card too — the row that could not be scanned", () => {
    // The reason this change exists. The chips render only when expanded, so a
    // score that also hid on collapse would leave the feed exactly as it was.
    const { container } = renderWithProviders(
      // `expanded` is REQUIRED, not optional: omitting it renders collapsed at
      // runtime and vitest is happy, but `pnpm run build` is the only
      // typechecker here and it is a TS2741. Say false out loud.
      <TriageCard call={scored} expanded={false} profileId={7} view="inbox" />,
    );
    expect(container.querySelector("[data-criterion]")).toBeNull();
    expect(container.querySelector('[data-field="score"]')?.textContent).toBe(
      "87",
    );
  });

  it("stays greyscale — no band, no pill", () => {
    // §5: colour is reserved for deadline urgency. [data-band] is
    // components/MatchBadge.tsx's own hook, so this catches the obvious
    // regression of reaching for the full edition's coloured pill.
    const { container } = renderWithProviders(
      <TriageCard call={scored} expanded profileId={7} view="inbox" />,
    );
    expect(container.querySelector("[data-band]")).toBeNull();
  });

  it("renders NO score when the pair has no ranking row", () => {
    // null is not zero. An unranked call must carry no score rather than a "0"
    // that reads as a judgement the ranker never made.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ aggregate_score: null })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="score"]')).toBeNull();
    expect(container.querySelector("article")?.textContent).not.toContain(
      "/ 100 match",
    );
  });
});

// -------------------------------------------------------------------------
// Gate 16 — THE REASON, ON THE ROW IT BELONGS TO (D2/D4, spec §5).
//
// The strip in the feed is deliberately ignorable, so most decisions arrive
// here with no note at all. A surface that only DISPLAYS an existing note is
// therefore a surface most people can never write on: the note has to be
// addable from the row too, and editable once written.
//
// Saved and Hidden only. A note hangs off a `call_decisions` row (D4) and the
// Inbox has none, so an affordance there would offer to annotate a decision
// that does not exist — and the endpoint would 404.
// -------------------------------------------------------------------------
describe("gate 16 — the decision note on the card", () => {
  function stubPut(note: string | null = null) {
    return vi.spyOn(clientMod.api, "PUT").mockResolvedValue({
      data: { decision_note: note },
      error: undefined,
      response: new Response(),
    } as never);
  }

  it("does not discard typing that happened while the save was in flight", async () => {
    // THE ACKNOWLEDGEMENT IS A REMOUNT. The editor is keyed on the note, and
    // `initialNote` is a mount-only seed -- so when the PUT lands and the list
    // refetch hands this card `decision_note: "First"`, the key changes, the
    // strip remounts, and everything typed after the Enter is replaced by the
    // value the server just confirmed. The user watches their own words vanish.
    stubPut("First");
    const props = { expanded: true, profileId: 7, view: "hidden" as const };
    const { rerender } = renderWithProviders(
      <TriageCard call={call({ decision_note: null })} {...props} />,
    );
    await userEvent.click(screen.getByRole("button", { name: /add note/i }));
    const box = screen.getByRole("textbox");
    await userEvent.type(box, "First{Enter}");
    await userEvent.type(box, " with more detail");
    // The write lands and the list refetch re-renders this row with the SAVED
    // note -- the everyday case, not a contrived one.
    rerender(<TriageCard call={call({ decision_note: "First" })} {...props} />);
    expect(screen.getByRole("textbox")).toHaveValue("First with more detail");
  });

  it("prints the note under the title in Hidden", () => {
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ decision_note: "wrong field" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    const shown = container.querySelector('[data-field="decision-note"]');
    expect(shown).not.toBeNull();
    expect(shown).toHaveTextContent("wrong field");
    // UNDER THE TITLE, not appended at the end of the card: the note answers
    // "why is this here" about the row it sits on, and a reason printed below
    // the evidence is read after the decision it explains.
    const title = container.querySelector("h2");
    expect(title?.compareDocumentPosition(shown as Node)).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("prints it in Saved too, and on a card that is not the focused one", () => {
    // The Save/Hide controls are outside the `expanded` block for the same
    // reason: an affordance only on the focused card means reaching any other
    // call requires moving the cursor first.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ decision_note: "worth a look in autumn" })}
        expanded={false}
        profileId={7}
        view="saved"
      />,
    );
    expect(
      container.querySelector('[data-field="decision-note"]'),
    ).toHaveTextContent("worth a look in autumn");
  });

  it("offers NOTHING in the Inbox — no note, no Add-note control", () => {
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ decision_note: null })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(screen.queryByRole("button", { name: /add note/i })).toBeNull();
    expect(container.querySelector('[data-field="decision-note"]')).toBeNull();
    expect(container.querySelector("[data-note-strip]")).toBeNull();
  });

  it("still offers nothing in the Inbox when a note somehow arrives on the row", () => {
    // The gate is on the VIEW, not on the null. Without this, deleting the
    // `view !== "inbox"` check and keeping only `decision_note !== null` passes
    // the test above for free — the API never sends a note on the inbox view,
    // so that fixture proves nothing about which condition is doing the work.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ decision_note: "leaked" })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="decision-note"]')).toBeNull();
    expect(screen.queryByText("leaked")).toBeNull();
  });

  it("offers 'Add note' on a hidden card that has none, and writing one reaches the endpoint", async () => {
    // THE REACHABILITY HALF. The feed's strip appears once, at the moment of
    // the decision, and is ignorable by design (D2) — so without a control
    // here, "I'll say why later" is not a thing the product can do at all. A
    // test that only checks the non-null path is green while that is true.
    const put = stubPut("because it duplicates 2024");
    renderWithProviders(
      <TriageCard
        call={call({ id: 42, decision_note: null })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /add note/i }));
    await userEvent.type(
      screen.getByRole("textbox"),
      "because it duplicates 2024{Enter}",
    );
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
        params: { path: { call_id: 42 }, query: { profile_id: 7 } },
        // expected_state comes from the ROW's view, not from the strip: the
        // server 409s a note filed against a decision that has since flipped.
        body: { note: "because it duplicates 2024", expected_state: "hidden" },
      }),
    );
  });

  it("puts the caret in the box it just opened, on both the add and edit paths", async () => {
    // NOT a nicety -- the fix for a silent data-loss path. Clicking either
    // control UNMOUNTS the button that was clicked, so document.activeElement
    // falls back to <body> while a text box is visibly open. The feed's window
    // listener guards on e.target (isEditableTarget), so with focus on <body>
    // it is live: `u` on that row sends DELETE /api/calls/{id}/hide and the
    // decision takes its note with it (D4), and `s` flips hidden -> saved,
    // which clears the note server-side (D9). Either way the reason the user
    // just opened is destroyed with no message.
    //
    // Both paths, because they are two call sites of the same swap and either
    // can be got wrong alone.
    stubPut("x");
    const { unmount } = renderWithProviders(
      <TriageCard
        call={call({ id: 42, decision_note: null })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /add note/i }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("textbox")),
    );
    unmount();

    renderWithProviders(
      <TriageCard
        call={call({ id: 43, decision_note: "wrong field" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "wrong field" }));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("textbox")),
    );
  });

  it("keeps the caret across the remount a successful save causes", async () => {
    // The SECOND half of the same gap, and it needs its own test because the
    // fix is a different line. The strip is keyed on the note (so an edited
    // note re-seeds the box), which means a successful save REMOUNTS it -- and
    // a focus set only on first open is lost exactly then, silently re-opening
    // the window above while the box still looks focused-and-open.
    const put = stubPut("saved once");
    const { rerender } = renderWithProviders(
      <TriageCard
        call={call({ id: 44, decision_note: "before" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "before" }));
    await userEvent.clear(screen.getByRole("textbox"));
    await userEvent.type(screen.getByRole("textbox"), "saved once{Enter}");
    await waitFor(() => expect(put).toHaveBeenCalled());
    // The server's new value arrives as a prop, which changes the key.
    rerender(
      <TriageCard
        call={call({ id: 44, decision_note: "saved once" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole("textbox")),
    );
  });

  it("edits an existing note through the same strip, seeded with what is already there", async () => {
    const put = stubPut("second thoughts");
    renderWithProviders(
      <TriageCard
        call={call({ id: 42, decision_note: "wrong field" })}
        expanded
        profileId={7}
        view="saved"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /wrong field/i }));
    const box = screen.getByRole("textbox");
    // SEEDED, not empty: a component with no way to receive the current value
    // can only ever create, so "edit" would silently be "replace from blank".
    expect(box).toHaveValue("wrong field");
    await userEvent.clear(box);
    await userEvent.type(box, "second thoughts{Enter}");
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
        params: { path: { call_id: 42 }, query: { profile_id: 7 } },
        body: { note: "second thoughts", expected_state: "saved" },
      }),
    );
  });

  it("re-seeds the open editor when the stored note changes under it", async () => {
    // THE STALE-SEED GATE. DecisionNoteStrip reads `initialNote` into useState
    // on MOUNT and never again — it is a seed, not a controlled value. The feed
    // gets away with that because it keys the strip per decision; a row does
    // not, because the row's note changes underneath the open editor.
    //
    // The everyday way that happens is normalisation: the strip trims before
    // sending (D8), so typing "  padded  " stores "padded", and the box that
    // is still open must show what was STORED, not the raw text that produced
    // it. Without a `key` covering the note the box keeps the padding for the
    // rest of the session and the next Enter re-sends a no-op.
    //
    // Entered through the EXISTING-note control rather than through "Add note",
    // so this gate and the reachability gate above fail for different reasons:
    // deleting the Add-note control must not redden this one too.
    const put = stubPut("padded reason");
    const { rerender } = renderWithProviders(
      <TriageCard
        call={call({ id: 42, decision_note: "first go" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /first go/i }));
    const box = screen.getByRole("textbox");
    await userEvent.clear(box);
    await userEvent.type(box, "   padded reason   {Enter}");
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith("/api/calls/{call_id}/note", {
        params: { path: { call_id: 42 }, query: { profile_id: 7 } },
        body: { note: "padded reason", expected_state: "hidden" },
      }),
    );
    // The invalidated ["calls"] query lands and the row now carries the STORED
    // note -- trimmed, unlike the raw text still sitting in the box. The editor
    // is still open.
    rerender(
      <TriageCard
        call={call({ id: 42, decision_note: "padded reason" })}
        expanded
        profileId={7}
        view="hidden"
      />,
    );
    // Compared as a RAW string rather than through toHaveValue: the failure has
    // to be legible, and jest-dom prints "   padded reason   " and "padded
    // reason" identically once the terminal has eaten the padding.
    await waitFor(() =>
      expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(
        "padded reason",
      ),
    );
  });

  it("writes nothing while there is no profile to write it against", () => {
    // A note lives on `call_decisions`, whose profile_id is NOT NULL. With no
    // profile resolved there is no row to annotate, so the control stands down
    // rather than firing a request that cannot be satisfied.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ decision_note: null })}
        expanded
        profileId={null}
        view="hidden"
      />,
    );
    expect(screen.queryByRole("button", { name: /add note/i })).toBeNull();
    expect(container.querySelector('[data-field="decision-note"]')).toBeNull();
  });
});

describe("the card marks a score the local model could not refresh", () => {
  // Review 2026-10-26 A4 finding 2: 13 open calls were showing a pre-upgrade
  // score as though it were current. THE CARD is the surface that prints the
  // number and the surface `s`/`h` are pressed from, so it is the surface the
  // reader is misled on.
  it("says so, with the date, right under the stale number", () => {
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          aggregate_score: 71,
          ranking_failed: true,
          ranking_failed_at: "2026-10-25T03:17:42Z",
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    // The stale score is STILL SHOWN -- it is all there is -- so the note has to
    // sit beside it rather than replace it.
    expect(container.querySelector('[data-field="score"]')).toHaveTextContent("71");
    const note = container.querySelector('[data-field="ranking-failed"]');
    expect(note).toHaveTextContent(
      "Couldn't rank this call with the local model (last tried 2026-10-25)",
    );
    // UNDER the score, not above it: the number is what the reader lands on and
    // the caveat has to arrive after it. `compareDocumentPosition`'s
    // FOLLOWING bit (4) is set when `note` comes later in the document.
    const score = container.querySelector('[data-field="score"]')!;
    expect(score.compareDocumentPosition(note!) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(4);
  });

  it("stays silent on a card whose ranking is current", () => {
    // The whole fixture is identical bar the one flag, so this cannot pass by
    // the note being unreachable for some other reason.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({ aggregate_score: 71, ranking_failed: false, ranking_failed_at: null })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="score"]')).toHaveTextContent("71");
    expect(container.querySelector('[data-field="ranking-failed"]')).toBeNull();
  });

  it("explains an ABSENT score too, rather than leaving a silent gap", () => {
    // A pair that never got a ranking at all renders no number (that is
    // deliberate -- null is not a zero), and before this the card said nothing
    // about why. The note is outside the score's own guard for that reason.
    const { container } = renderWithProviders(
      <TriageCard
        call={call({
          aggregate_score: null,
          ranking_failed: true,
          ranking_failed_at: "2026-10-25T03:17:42Z",
        })}
        expanded
        profileId={7}
        view="inbox"
      />,
    );
    expect(container.querySelector('[data-field="score"]')).toBeNull();
    expect(container.querySelector('[data-field="ranking-failed"]')).toHaveTextContent(
      "last tried 2026-10-25",
    );
  });
});
