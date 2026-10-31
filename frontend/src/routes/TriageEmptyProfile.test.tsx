import { fireEvent, screen, waitFor } from "@testing-library/react";
import { useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";

import { renderWithProviders } from "../test-utils";
import { TriageEmptyProfile } from "./TriageEmptyProfile";

// THE TWO STATES OF THE SCREEN, and they are different screens, not one screen
// with a conditional line: a user with no profile at all must be sent to
// /profiles/new, and a user who HAS profiles must be offered them rather than
// pushed into creating a duplicate (brief step 1, gate 3).

it("with no profiles, invites the user to create one and says what a profile is for", () => {
  const { container } = renderWithProviders(<TriageEmptyProfile profiles={[]} />);

  expect(screen.getByRole("heading", { name: "No research profile yet." })).toBeInTheDocument();
  // The SENTENCE, not just the heading: "says what a profile is for in one
  // sentence" is the requirement, and a bare headline over a bare link does not
  // tell a first-time user what they are being asked to make.
  expect(container.querySelector('[data-field="why"]')).toHaveTextContent(
    "A research profile describes what your group works on, and every call is matched against it.",
  );
  expect(screen.getByRole("link", { name: /create a research profile/i })).toHaveAttribute(
    "href",
    "/profiles/new",
  );
  // Gate 3, the OTHER branch: /profiles/new ALONE here. Asserting the picker's
  // absence is what stops the has-profiles branch from being the only branch —
  // a screen that always rendered a (empty) picker would still pass the link
  // assertion above.
  expect(container.querySelector('[data-field="profile-picker"]')).toBeNull();
  expect(container.querySelectorAll("[data-profile-pick]")).toHaveLength(0);
});

it("with profiles, lists them as links to that profile's feed", () => {
  const { container } = renderWithProviders(
    <TriageEmptyProfile
      profiles={[
        { id: 7, name: "Molecular oncology" },
        { id: 9, name: "Clinical genomics" },
      ]}
    />,
  );

  expect(screen.getByRole("heading", { name: "Pick a research profile." })).toBeInTheDocument();
  const picks = [...container.querySelectorAll("[data-profile-pick]")];
  // BOTH the count and the hrefs: a picker that renders one row for the first
  // profile only, or one that links every row to the same id, passes a
  // `getByText("Molecular oncology")` check.
  expect(picks.map((el) => el.textContent)).toEqual(["Molecular oncology", "Clinical genomics"]);
  expect(picks.map((el) => el.getAttribute("href"))).toEqual(["/?profile=7", "/?profile=9"]);
  // Create is still offered — the requirement is "not /profiles/new ALONE", not
  // "no /profiles/new".
  expect(screen.getByRole("link", { name: /create another profile/i })).toHaveAttribute(
    "href",
    "/profiles/new",
  );
});

it("is a landmark of its own, because SimpleShell deliberately renders no <main>", () => {
  // SimpleShell.tsx — "A plain div, NOT <main>: every release-1 page renders its
  // own <main>". The feed does; this screen REPLACES the feed, so it must too,
  // or the match edition's first screen has no main landmark at all and the
  // browser walk's `toHaveCount(1)` on / goes red.
  renderWithProviders(<TriageEmptyProfile profiles={[]} />);
  expect(screen.getByRole("main")).toBeInTheDocument();
});

// --- the pick is remembered (UX pass 2026-10-31) -----------------------------
// Everyone who did not create the profile they use landed on this screen on
// every visit: the pick was a bare link and nothing stored it. A plain click is
// the user's explicit answer, so it is saved as their default; the href stays,
// so a new tab (or any ?profile= link) writes nothing.

afterEach(() => {
  vi.restoreAllMocks();
});

function Where() {
  const loc = useLocation();
  return <span data-testid="where">{loc.pathname + loc.search}</span>;
}

const TWO = [
  { id: 7, name: "Molecular oncology" },
  { id: 9, name: "Clinical genomics" },
];

it("a click on a profile saves it as the default, then opens its feed", async () => {
  const post = vi
    .spyOn(clientMod.api, "POST")
    .mockResolvedValue({ data: { default_profile_id: 9 }, error: undefined, response: new Response() } as never);
  renderWithProviders(
    <>
      <TriageEmptyProfile profiles={TWO} />
      <Where />
    </>,
  );
  fireEvent.click(screen.getByRole("link", { name: "Clinical genomics" }));
  await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/?profile=9"));
  expect(post).toHaveBeenCalledWith("/api/me/default-profile", { body: { profile_id: 9 } });
});

it("a failed save still opens the feed: the pick works for this visit", async () => {
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({
    data: undefined,
    error: { error: { code: "internal_error", message: "down" } },
    response: new Response(null, { status: 500 }),
  } as never);
  renderWithProviders(
    <>
      <TriageEmptyProfile profiles={TWO} />
      <Where />
    </>,
  );
  fireEvent.click(screen.getByRole("link", { name: "Molecular oncology" }));
  await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent("/?profile=7"));
});

it("a ctrl-click (open in a new tab) saves nothing and leaves the browser to it", () => {
  const post = vi.spyOn(clientMod.api, "POST");
  renderWithProviders(<TriageEmptyProfile profiles={TWO} />);
  // jsdom cannot open tabs; stop its navigation AFTER the component has seen
  // the event (document-level listeners run after React's root listener).
  const stopJsdom = (e: Event) => e.preventDefault();
  document.addEventListener("click", stopJsdom, { once: true });
  fireEvent.click(screen.getByRole("link", { name: "Molecular oncology" }), { ctrlKey: true });
  expect(post).not.toHaveBeenCalled();
});
