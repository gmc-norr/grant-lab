// frontend/src/routes/CallRow.test.tsx
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import type { CallItem } from "../api/dashboard";
import { theme } from "../theme/theme";
import { CallRow } from "./CallRow";

// CallRow's destination is edition-dependent, and isMatchEdition is a BUILD-TIME
// constant (edition.ts reads import.meta.env at module scope), so a test that
// read the real value would pass or fail by environment. Mocked with the mutable
// holder + GETTER pattern from AdminNav.test.tsx:25-34 — the getter is
// load-bearing: CallRow reads isMatchEdition during render and vitest's SSR
// transform rewrites that read to a namespace property access, so flipping
// `edition.isMatch` between tests is observed. A plain value would be frozen at
// import time and the match cases below would silently run the FULL branch and
// still pass their "does not contain selected=" style assertions only by luck —
// so both directions are asserted positively.
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

// A FULL literal, not a partial behind `as CallItem`. The assertion was a hole
// in the only typechecker this project has: `pnpm run build` runs tsc over
// `src`, and a cast tells it to stop looking -- so every field CallOut has
// gained since this file was written was missing here and nothing said so.
// Concretely, when `decision_note` landed on CallOut (2026-09-07), a
// null-guarded read of `call.decision_note` typechecked against a fixture that
// supplied `undefined` -- a value the API cannot produce -- and a row that
// rendered its note only for `!== null` would have been green here while being
// wrong in production. Keep it a literal: the compile error IS the reminder.
function call(over: Partial<CallItem> = {}): CallItem {
  return {
    id: 3,
    title: "Grant X",
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: null,
    deadline_local_date: null,
    published_at: null,
    aggregate_score: 82,
    programme: null,
    budget_min: null,
    budget_max: null,
    currency: null,
    url: "https://example.org/calls/3",
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

function renderRow(ui: React.ReactElement, url = "/") {
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter>
    </MantineProvider>,
  );
}

it("links the title to the reading-pane selection URL, shows source + match badge", () => {
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).not.toContain("profile=");
  expect(screen.getByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("82")).toHaveAttribute("data-band", "strong");
});

it("pins the resolved profile into the selection URL", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("profile=7");
});

it("preserves existing list filters/pagination in the selection URL", () => {
  // Guards the core selectionSearch contract at the CallRow level: a row that
  // forgot to pass the live `sp` would still pass the helper unit tests.
  renderRow(<CallRow call={call()} profileId={7} />, "/?source=eu&page=2&q=dna");
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toContain("selected=3");
  expect(href).toContain("source=eu");
  expect(href).toContain("page=2");
  expect(href).toContain("q=dna");
  expect(href).toContain("profile=7");
});

it("marks the row as current when it is the selected call", () => {
  renderRow(<CallRow call={call()} />, "/?selected=3");
  expect(screen.getByRole("listitem")).toHaveAttribute("aria-current", "true");
});

it("is not current when a different call is selected", () => {
  renderRow(<CallRow call={call()} />, "/?selected=99");
  expect(screen.getByRole("listitem")).not.toHaveAttribute("aria-current");
});

it("clicking empty row area selects the call", () => {
  function Probe() {
    return <span data-testid="search">{useLocation().search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="/" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
    </Routes>,
  );
  fireEvent.click(screen.getByRole("listitem"));
  expect(screen.getByTestId("search").textContent).toContain("selected=3");
  expect(screen.getByTestId("search").textContent).toContain("profile=7");
});

it("the FULL edition renders exactly two links, and Start drafting is the second", () => {
  // The row's onClick guard (`closest("a,button")`) is observable again: clicking
  // Start drafting must NOT select the row, it must leave for /cockpit/new. When
  // the link was deleted the title anchor's href WAS the selection URL, so the
  // guard changed nothing a test could see and this file said so. It is real
  // coverage again.
  function Probe() {
    const loc = useLocation();
    return <span data-testid="loc">{loc.pathname + loc.search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="*" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
    </Routes>,
  );
  const links = screen.getAllByRole("link");
  expect(links).toHaveLength(2);
  expect(links[0]).toHaveAccessibleName("Grant X");
  expect(links[1]).toHaveAccessibleName("Start drafting");
  fireEvent.click(links[1]);
  const loc = screen.getByTestId("loc").textContent ?? "";
  expect(loc.split("?")[0]).toBe("/cockpit/new");
  expect(loc).not.toContain("selected=");
});

it("the FULL edition prefills the call and the resolved profile into Start drafting", () => {
  renderRow(<CallRow call={call()} profileId={7} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).toContain("/cockpit/new?");
  expect(href).toContain("call_id=3");
  expect(href).toContain("call_title=Grant+X");
  expect(href).toContain("profile_id=7");
});

it("the FULL edition omits profile_id from Start drafting when no profile is resolved", () => {
  // The no-profile branch builds a DIFFERENT href, so it is pinned separately: a
  // regression that always appended profile_id=null would pass the test above.
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Start drafting" }).getAttribute("href") ?? "";
  expect(href).toContain("call_id=3");
  expect(href).not.toContain("profile_id");
});

it("the MATCH edition renders NO link into /cockpit, with or without a profile", () => {
  // Release 1 does not mount /cockpit/new, so this link would be a 404 button.
  // Asserting on the NAME alone would pass on a row that renamed the link, so
  // every rendered href is checked too — and the link COUNT is pinned, so a
  // second link of any name fails here.
  for (const profileId of [7, undefined]) {
    edition.isMatch = true;
    const { unmount } = renderRow(<CallRow call={call()} profileId={profileId} />);
    expect(screen.queryByRole("link", { name: /start drafting/i })).not.toBeInTheDocument();
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    for (const a of links) expect(a.getAttribute("href") ?? "").not.toContain("/cockpit");
    unmount();
  }
});

it("omits the match badge when the score is null", () => {
  renderRow(<CallRow call={call({ aggregate_score: null })} />);
  expect(screen.queryByText("82")).toBeNull();
});

// --- Match edition -----------------------------------------------------------
// Release-1 CRITICAL: router.simple.tsx mounts no reading pane, so ?selected=N
// renders nothing and /calls/:id — which it DOES mount — was reachable from no
// link at all. These pin the match branch; the full-edition tests above are the
// control that catches a fix applied unconditionally.

it("the MATCH edition links the title to the call's own page, not a selection URL", () => {
  edition.isMatch = true;
  renderRow(<CallRow call={call()} />);
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toBe("/calls/3");
  expect(href).not.toContain("selected=");
});

it("the MATCH edition pins the resolved profile onto the detail URL", () => {
  // CallDetail.tsx:13-20 reads ?profile= and ranks against it, exactly as
  // CallReadingPane.tsx:50's "Open full page" link relies on.
  edition.isMatch = true;
  renderRow(<CallRow call={call()} profileId={7} />, "/?source=eu&page=2");
  const href = screen.getByRole("link", { name: "Grant X" }).getAttribute("href") ?? "";
  expect(href).toBe("/calls/3?profile=7");
});

it("the MATCH edition navigates to the call page when the row body is clicked", () => {
  edition.isMatch = true;
  function Probe() {
    const loc = useLocation();
    return <span data-testid="loc">{loc.pathname + loc.search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="/" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
      <Route path="/calls/:id" element={<Probe />} />
    </Routes>,
  );
  fireEvent.click(screen.getByRole("listitem"));
  expect(screen.getByTestId("loc").textContent).toBe("/calls/3?profile=7");
});

it("the FULL edition still selects into the reading pane on a row click", () => {
  // The control. Without it a fix that dropped the edition gate — sending the
  // full edition to /calls/:id too — would leave every match test above green.
  edition.isMatch = false;
  function Probe() {
    const loc = useLocation();
    return <span data-testid="loc">{loc.pathname + loc.search}</span>;
  }
  renderRow(
    <Routes>
      <Route path="/" element={<><CallRow call={call()} profileId={7} /><Probe /></>} />
    </Routes>,
  );
  fireEvent.click(screen.getByRole("listitem"));
  const loc = screen.getByTestId("loc").textContent ?? "";
  expect(loc).toContain("selected=3");
  expect(loc.split("?")[0]).toBe("/");
});

it("says when the local model could not refresh the score behind the pill", () => {
  // Review 2026-10-26 A4 finding 2. The band pill is computed from
  // `aggregate_score`, so a stale score makes a stale BAND -- the row is as
  // misleading as the match feed's card and carries the same sentence.
  const { container } = renderRow(
    <CallRow
      call={call({ ranking_failed: true, ranking_failed_at: "2026-10-25T03:17:42Z" })}
      profileId={4}
    />,
  );
  expect(container.querySelector('[data-field="ranking-failed"]')).toHaveTextContent(
    "Couldn't rank this call with the local model (last tried 2026-10-25)",
  );
  // The control: the same row with a current ranking says nothing.
  const clean = renderRow(<CallRow call={call()} profileId={4} />);
  expect(clean.container.querySelector('[data-field="ranking-failed"]')).toBeNull();
});
