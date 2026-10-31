import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import * as clientMod from "../api/client";
import type { components } from "../api/schema.gen";
import { renderWithProviders } from "../test-utils";
import { AdminDashboard } from "./AdminDashboard";

// The "Manage golden set" link is NO LONGER gated on the edition: router.simple
// .tsx mounts /admin/golden-set in the match edition as of 2026-09-07, so a gate
// here would hide a page release 1 can reach. The two tests at the bottom of this
// file used to pin the gating; they now pin its ABSENCE, by flipping the edition
// and asserting the link is present either way. Keeping both directions is the
// point: a single un-flipped case would pass again the moment a guard came back.
// See AdminNav.test.tsx for why the mock uses a getter.
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
  vi.restoreAllMocks();
});

const DASH: components["schemas"]["AdminDashboardOut"] = {
  sources: [
    {
      name: "Vinnova",
      slug: "vinnova",
      last_fetched_at: null,
      call_count: 3,
      consecutive_failures: 0,
      status: "never_fetched",
    },
  ],
  queue_counts: { pending: 2, running: 0, done: 5, failed: 1 },
  rerank_estimate: 6,
  active_profiles_count: 2,
  open_calls_count: 3,
  digest: {
    last_sent_at: null,
    runs_this_week: 0,
    disabled_users: 1,
    disabled_user_rows: [{ id: 9, email: "bounced@umu.se", disabled_at: "2026-06-01T00:00:00Z" }],
  },
  embedding_coverage: {
    call_embedded: 1, call_total: 3, call_stale: 2,
    profile_embedded: 1, profile_total: 2, profile_stale: 1, current_model: "bge-m3",
  },
  golden_set_report: null,
  calibration_warning: null,
};

function mockDash(data: unknown = DASH) {
  return vi.spyOn(clientMod.api, "GET").mockResolvedValue({ data, error: undefined } as never);
}

it("renders source health, queue counts and coverage", async () => {
  mockDash();
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText("bounced@umu.se")).toBeInTheDocument();
  expect(screen.getByText(/1\/3 embedded/)).toBeInTheDocument();
  expect(screen.getByText(/Enqueues 6 ranking jobs/)).toBeInTheDocument();
});

it("renders a clean error state when the read-model fails", async () => {
  vi.spyOn(clientMod.api, "GET").mockResolvedValue({
    data: undefined, error: { error: { code: "internal_error", message: "boom" } },
    response: { status: 500 },
  } as never);
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't load/i);
});

it("fetch-now posts the slug and shows the ok banner", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: true }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/sources/{slug}/fetch", {
      params: { path: { slug: "vinnova" } },
    }),
  );
  expect(await screen.findByText(/fetch triggered/i)).toBeInTheDocument();
});

it("fetch-now shows the err banner when not triggered", async () => {
  mockDash();
  vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { triggered: false }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /fetch now/i }));
  expect(await screen.findByText(/couldn't trigger the fetch/i)).toBeInTheDocument();
});

it("rerank confirms before posting", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { enqueued: 2 }, error: undefined } as never);
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-rank everything/i }));
  // An upper bound, not an exact count: pairs already queued are skipped
  // (ON CONFLICT DO NOTHING in ranking/backfill.py).
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining("up to 6 ranking jobs"));
  expect(post).not.toHaveBeenCalled();  // declined -> no POST
});

it("reenable posts the user id", async () => {
  mockDash();
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { id: 9 }, error: undefined } as never);
  renderWithProviders(<AdminDashboard />);
  await userEvent.click(await screen.findByRole("button", { name: /re-enable/i }));
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith("/api/admin/digest/reenable", { body: { user_id: 9 } }),
  );
});

it("the FULL edition links out to the golden-set admin page", async () => {
  // The other direction. Together with the match case below, the pair says the
  // link is edition-INDEPENDENT: either one alone would pass on a component that
  // had reintroduced a guard pointing the other way.
  edition.isMatch = false;
  mockDash();
  renderWithProviders(<AdminDashboard />);
  expect(await screen.findByRole("link", { name: /manage golden set/i })).toHaveAttribute(
    "href",
    "/admin/golden-set",
  );
});

it("the MATCH edition links out to the golden-set page too, now that it is mounted", async () => {
  // The inversion of the 2026-09-04 assertion, which read "renders no link to the
  // unmounted golden-set page". The page is mounted now (router.simple.test.tsx's
  // RELEASE_1_ROUTES) and the API behind it is in _ALWAYS, so hiding the link
  // would strand the only screen that shows the human reasons on the candidates.
  edition.isMatch = true;
  mockDash();
  renderWithProviders(<AdminDashboard />);
  // Wait for the page to actually render first, so this is not a claim about a
  // still-loading screen.
  expect(await screen.findByText("Vinnova")).toBeInTheDocument();
  expect(screen.getByText(/Ranking quality/)).toBeInTheDocument(); // the card is still there
  expect(screen.getByRole("link", { name: /manage golden set/i })).toHaveAttribute(
    "href",
    "/admin/golden-set",
  );
});


// Beta review 2026-10-31 F8: the failure streak was stored but never shown,
// and email alerts are not configured for the beta, so this column is the only
// place a dead source becomes visible.
it("names each source's state, with the failure streak", async () => {
  mockDash({
    ...DASH,
    sources: [
      { name: "EU portal", slug: "eu_portal", last_fetched_at: "2026-10-27T02:00:00Z",
        call_count: 600, consecutive_failures: 2, status: "failing" },
      { name: "Vinnova", slug: "vinnova", last_fetched_at: "2026-10-20T02:15:00Z",
        call_count: 90, consecutive_failures: 0, status: "stale" },
      { name: "Other", slug: "other", last_fetched_at: "2026-10-31T02:00:00Z",
        call_count: 5, consecutive_failures: 0, status: "ok" },
    ],
  });
  const { container } = renderWithProviders(<AdminDashboard />);
  await screen.findByText("EU portal");
  const status = (slug: string) =>
    container.querySelector(`[data-source-status="${slug}"]`)?.textContent;
  expect(status("eu_portal")).toBe("Failing · 2 in a row");
  expect(status("vinnova")).toBe("Stale");
  expect(status("other")).toBe("OK");
});
