import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// THE MATCH EDITION'S ONLY BROWSER COVERAGE. Every other gate on this branch is
// a unit test or a static scan, and the two worst defects the branch shipped —
// no way to sign out at all, and a call list whose rows navigated nowhere —
// were both invisible to all of them and obvious in a browser within seconds.
//
// It runs against the REAL match bundle: playwright.config.ts's E2E_MODE=match
// webServer runs `pnpm build:match && pnpm check:bundle` and then serves
// frontend/dist. So the artefact under test is a bundle that PASSED gate 1, not
// a dev server and not a re-render of the components in isolation.
//
// The backend is mocked at the network boundary (page.route) on purpose. What
// distinguishes this edition is which router compiled in, which links render,
// and which palette paints — none of which needs Postgres, and requiring one is
// why this coverage did not exist. Route-surface parity between the match SPA
// and the match backend is a different question, answered by
// tests/architecture/test_edition_surface.py.
//
// Routes release 1 excludes, used below and kept in step with
// src/router.simple.test.tsx and src/noCockpitLinks.test.tsx.
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/golden-set",
  "/admin/deleted-workspaces",
];

const ME = {
  user: { id: 1, email: "p@umu.se", name: "P", status: "active", default_profile_id: null },
  capabilities: { admin: true },
};

const CALLS = {
  items: [
    {
      id: 1,
      title: "Genomics Grant",
      source_name: "Vinnova",
      source_slug: "vinnova",
      deadline: null,
      published_at: null,
      aggregate_score: 82,
    },
  ],
  summary: { open_count: 1, high_score: 1, soon_deadline: 0 },
  pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 1 },
  ranking: { pending: 0, failed: 0 },
  effective: { profile_id: null, source: null, sort: "newest", show_closed: false },
  facets: { sources: [], profiles: [] },
};

const CALL_DETAIL = {
  call: {
    id: 1,
    title: "Genomics Grant",
    source_name: "Vinnova",
    programme: "Prog",
    external_id: "EX-1",
    published_at: null,
    deadline: null,
    budget_min: null,
    budget_max: null,
    currency: null,
    url: "https://example.invalid/call",
    keywords: ["dna"],
    summary: "Sum.",
  },
  ranking: null,
  criteria: [],
  best_profile_id: null,
};

// The admin landing page's read-model. A real payload, not `{}`: the catch-all
// below answers 200 {} and AdminDashboard then crashes on `sources.map`, which
// takes react-router's DEFAULT error boundary — neither router declares an
// errorElement — and blanks the whole route subtree including the subnav. Worth
// knowing: in this app any render error inside a route replaces the page with
// react-router's developer error screen. That is pre-existing and identical in
// the full edition; it is recorded, not fixed here.
const ADMIN_DASH = {
  sources: [{ name: "Vinnova", slug: "vinnova", last_fetched_at: null, call_count: 3 }],
  queue_counts: { pending: 0, running: 0, done: 5, failed: 0 },
  rerank_estimate: 6,
  active_profiles_count: 2,
  recent_calls_count: 3,
  backfill_days: 90,
  digest: { last_sent_at: null, runs_this_week: 0, disabled_users: 0, disabled_user_rows: [] },
  embedding_coverage: {
    call_embedded: 1,
    call_total: 3,
    call_stale: 2,
    profile_embedded: 1,
    profile_total: 2,
    profile_stale: 1,
    current_model: "bge-m3",
  },
  golden_set_report: null,
  calibration_warning: null,
};

// Every /api/** call the match bundle can make is answered here. The catch-all
// returns 200 {} rather than falling through to the static server, because an
// unmatched call would get index.html, and `unwrap` turns a non-JSON or 401
// response into a full-page nav to /login — a spec that silently ended up on the
// login page would then "pass" its absence assertions.
async function mockApi(page: Page, seen: string[] = []): Promise<string[]> {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    seen.push(`${route.request().method()} ${url.pathname}`);
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/me") return json(ME);
    if (url.pathname === "/api/calls") return json(CALLS);
    if (/^\/api\/calls\/\d+$/.test(url.pathname)) return json(CALL_DETAIL);
    if (url.pathname === "/api/admin/dashboard") return json(ADMIN_DASH);
    if (url.pathname === "/api/auth/logout") return json({});
    if (url.pathname === "/api/auth/config") return json({ oidc_enabled: false });
    return json({});
  });
  return seen;
}

async function hrefsOn(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll("a[href]")].map((a) => a.getAttribute("href") ?? ""),
  );
}

test("the release-1 shell renders, and the dashboard lists calls", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Call Match" })).toBeVisible();
  // The SimpleShell nav, not the full edition's activity-bar rail.
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: "Calls" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Profiles" })).toBeVisible();
  await expect(page.getByRole("listitem").first()).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "Genomics Grant" })).toBeVisible();
});

test("clicking a call row opens that call's own page", async ({ page }) => {
  // Release-1 CRITICAL, shipped and caught by hand: the simple router mounts no
  // reading pane, so the row's ?selected=N link changed the query string and
  // NOTHING happened. /calls/:id was mounted and reachable from no link at all.
  await mockApi(page);
  await page.goto("/");
  await page.getByRole("link", { name: "Genomics Grant" }).click();
  await expect(page).toHaveURL(/\/calls\/1(\?|$)/);
  await expect(page.getByRole("heading", { name: "Genomics Grant" })).toBeVisible();
});

test("signing out reaches the logout endpoint and leaves for /login", async ({ page }) => {
  // Release-1 CRITICAL, shipped and caught by hand: the full edition's only
  // sign-out control lives in TitleBar.tsx, which gate 1 excludes from the match
  // graph — so release 1 had NO way to end a session, on shared workstations.
  const seen = await mockApi(page);
  await page.goto("/");
  await page.getByRole("button", { name: /account menu for p@umu\.se/i }).click();
  await page.getByRole("menuitem", { name: /sign out/i }).click();
  await page.waitForURL(/\/login$/, { timeout: 10_000 });
  // The navigation alone is not proof: a control that only did
  // location.assign("/login") without ending the server session would pass it.
  expect(seen).toContain("POST /api/auth/logout");
});

test("no rendered link anywhere in release 1 points into the excluded half", async ({ page }) => {
  // The browser-level counterpart to src/noCockpitLinks.test.tsx, which is a
  // SOURCE scan and now EXEMPTs CallRow.tsx and CallDetailBody.tsx (their
  // /cockpit link is edition-gated, and a scan cannot tell a gated literal from
  // an ungated one). This asks the rendered DOM instead.
  await mockApi(page);
  const offenders: string[] = [];
  for (const path of ["/", "/calls/1", "/admin"]) {
    await page.goto(path);
    await expect(page.getByRole("link", { name: "Call Match" })).toBeVisible();
    for (const href of await hrefsOn(page)) {
      if (EXCLUDED_PREFIXES.some((p) => href.startsWith(p))) offenders.push(`${path} -> ${href}`);
    }
  }
  expect(offenders).toEqual([]);
});

test("the admin subnav offers only the three sections release 1 mounts", async ({ page }) => {
  await mockApi(page);
  await page.goto("/admin");
  const nav = page.getByRole("navigation", { name: "Admin sections" });
  await expect(nav).toBeVisible({ timeout: 10_000 });
  expect(await nav.locator("a[href]").evaluateAll((as) =>
    as.map((a) => a.getAttribute("href") ?? ""),
  )).toEqual(["/admin", "/admin/users", "/admin/users/pending"]);
});

test("an excluded route renders the in-app soft 404, not a cockpit", async ({ page }) => {
  await mockApi(page);
  await page.goto("/cockpit/new");
  await expect(page.getByRole("alert")).toContainText("Not found");
  // The shell is still there — a soft 404 inside the app, not a blank document.
  await expect(page.getByRole("link", { name: "Call Match" })).toBeVisible();
});

test("the light ground is actually PAINTED, not merely declared", async ({ page }) => {
  // theme.match.css was once completely inert — it defined the tokens at :root,
  // theme.css defined the same ones at :root and won on source order, and all
  // nine contrast unit tests stayed green because they scraped the FILE. Only a
  // browser can answer this. --app-editor: #faf9f7 is painted by
  // SimpleShell.module.css's .shell background.
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Call Match" })).toBeVisible();
  const bg = await page.evaluate(() => {
    const shell = document.querySelector("header")?.parentElement;
    return shell ? getComputedStyle(shell).backgroundColor : null;
  });
  expect(bg).toBe("rgb(250, 249, 247)");
});

test("exactly one main landmark per page, and no critical a11y violations", async ({ page }) => {
  // The landmark count is pinned explicitly rather than left to axe: SimpleShell
  // shipped a <main> wrapper around <Outlet/> while all twelve release-1 pages
  // render their own, and the unit test that should have caught it asserted
  // getByRole("main") against a harness child of <p>calls page</p> — so it
  // passed only because the fake child had no landmark.
  await mockApi(page);
  for (const path of ["/", "/calls/1"]) {
    await page.goto(path);
    await expect(page.getByRole("link", { name: "Call Match" })).toBeVisible();
    expect(await page.locator("main").count(), `main landmarks on ${path}`).toBe(1);
  }
  await page.goto("/");
  await expect(page.getByRole("listitem").first()).toBeVisible({ timeout: 10_000 });
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});
