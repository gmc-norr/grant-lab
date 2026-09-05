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
      deadline_local_date: null,
      published_at: null,
      aggregate_score: 82,
      // THE CARD'S OWN FIELDS. This fixture was written for CallRow, which read
      // none of them; TriageCard calls `call.criteria.find(...)` and
      // `call.keywords.map(...)` unconditionally, so an item without them takes
      // the whole route into react-router's error boundary. MEASURED on this
      // branch the moment the feed was mounted: 8 failed / 6 passed, every
      // failure reported as "/ crashed" rather than as anything about the feed.
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
    },
  ],
  summary: { open_count: 1, high_score: 1, soon_deadline: 0 },
  pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: 1 },
  ranking: { pending: 0, failed: 0 },
  // A RESOLVED PROFILE. `profile_id: null` was the shipped shape here while the
  // index was the archive list, where it meant "no profile filter". The triage
  // feed reads it as "there is nothing to record a decision on": the rail's
  // counts are not even requested, and every decision control stands down. A
  // fixture in that state exercises none of the screen this task added.
  effective: { profile_id: 1, source: null, sort: "newest", show_closed: false },
  facets: { sources: [], profiles: [{ id: 1, name: "Group A" }] },
};

// The rail's counts. The catch-all's `{}` would render three blank counts rather
// than crash, so this is realism, not a fix — but a blank rail is not what the
// a11y and landing specs below are meant to be looking at.
const DECISION_COUNTS = { inbox: 1, saved: 0, hidden: 0 };

// The OTHER index screen: no profile resolved. Release 1's answer is an
// invitation, NOT the fifty-call archive. It keeps CALLS' item deliberately —
// the server really does answer with calls in this state — so the scenario below
// can assert the browser renders none of them.
const CALLS_NO_PROFILE = {
  ...CALLS,
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

// Every release-1 page's read-model. These exist so the WALK below can visit all
// fifteen mounted routes: a page whose fetch returns the wrong shape crashes into
// react-router's default error screen, and a crashed page has no links and no
// landmark — so an href check over it would pass vacuously. The walk asserts the
// page rendered before it asserts anything about the page.
const SETTINGS = {
  name: "P",
  email: "p@umu.se",
  is_admin: true,
  google_oidc_enabled: false,
  google_linked: false,
  has_password: true,
  profiles: [{ id: 1, name: "Group A", subscribed: true }],
};

const PROFILE = {
  id: 1,
  name: "Digital Pathology Lab",
  kind: "organizational_unit",
  level: "group",
  description: "Imaging research.",
  budget_currency: "SEK",
  areas: ["oncology"],
  budget_min: 100,
  budget_max: 200,
  trl_min: 3,
  trl_max: 7,
  must_include: [],
  hard_exclusions: [],
  org_type: "university_hospital",
  capabilities: [],
  patient_populations: [],
  scale: null,
  career_stage: null,
  role: null,
  years_since_phd: null,
  applicant_types: [],
  funding_instruments: ["project_grant"],
  funder_exclusions: [],
  country: "SE",
  host_institution: "UmU",
  can_edit: true,
  // The detail payload always carries it now, and the owner's sharing card
  // reads it — a fixture without it would walk /profiles/1 through a branch the
  // API cannot actually produce.
  visibility: "private",
  bootstrap_status: null,
  bootstrap_pubmed_status: null,
  publications_fetched: null,
};

const PROFILES = {
  profiles: [
    {
      id: 1,
      name: "Digital Pathology Lab",
      kind: "organizational_unit",
      level: "group",
      areas_count: 1,
      budget_currency: "SEK",
      description: "Imaging research.",
    },
  ],
};

// GET /api/profiles/{id}/viewers. The owner's sharing card asks for this on every
// mount of a PRIVATE profile, and the catch-all's `{}` would answer it with a list
// of nobody — which is the one thing the card must never say when it does not know.
const VIEWERS = {
  viewers: [{ user_id: 7, name: "Ada Lovelace", email: "ada@umu.se" }],
};
// The person the ADD scenario shares with. Not in VIEWERS, so "Grace Hopper" on the
// page can only have come from the response to the write.
const GRACE = { user_id: 8, name: "Grace Hopper", email: "grace@umu.se" };

const ADMIN_USERS = {
  users: [{ id: 1, email: "p@umu.se", name: null, status: "active", is_admin: true }],
  page: 1,
  per_page: 50,
  total: 1,
};

const ADMIN_PENDING = {
  users: [
    {
      id: 2,
      email: "new@umu.se",
      name: null,
      created_at: "2026-06-01T00:00:00Z",
      google_linked: false,
    },
  ],
  page: 1,
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
// The decision surface, whose QUERY STRING is half of what the request means:
// `profile_id` is what makes a decision per-profile, and a client that dropped
// it would look identical in a path-only log while 422-ing against the server.
const DECISION_PATH = /^\/api\/calls\/(?:\d+\/(?:save|hide)|decisions\/counts)$/;

async function mockApi(page: Page, seen: string[] = []): Promise<string[]> {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const query = DECISION_PATH.test(url.pathname) ? url.search : "";
    seen.push(`${route.request().method()} ${url.pathname}${query}`);
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/me") return json(ME);
    if (url.pathname === "/api/calls") return json(CALLS);
    if (url.pathname === "/api/calls/decisions/counts") return json(DECISION_COUNTS);
    if (/^\/api\/calls\/\d+\/save$/.test(url.pathname)) return json({ state: "saved" });
    if (/^\/api\/calls\/\d+\/hide$/.test(url.pathname)) return json({ hidden: true });
    if (/^\/api\/calls\/\d+$/.test(url.pathname)) return json(CALL_DETAIL);
    if (url.pathname === "/api/admin/dashboard") return json(ADMIN_DASH);
    if (url.pathname === "/api/admin/users") return json(ADMIN_USERS);
    if (url.pathname === "/api/admin/users/pending") return json(ADMIN_PENDING);
    if (url.pathname === "/api/me/settings") return json(SETTINGS);
    if (url.pathname === "/api/profiles") return json(PROFILES);
    if (/^\/api\/profiles\/\d+$/.test(url.pathname)) return json(PROFILE);
    if (/^\/api\/profiles\/\d+\/viewers$/.test(url.pathname)) return json(VIEWERS);
    if (url.pathname === "/api/auth/logout") return json({});
    // AuthConfigOut: Login/Register read allowed_domains[0], so `{}` crashes the
    // page into the error boundary — which the walk below catches rather than
    // passing over, because a crashed page has no links and no landmark.
    if (url.pathname === "/api/auth/config")
      return json({ allowed_domains: ["umu.se"], google_oidc_enabled: false });
    return json({});
  });
  return seen;
}

// Every concrete path router.simple.tsx mounts. Kept in step with
// src/router.simple.test.tsx's RELEASE_1_ROUTES, with the :id params filled in.
// The earlier version of the walk below visited three of these, while its own
// test title said "anywhere in release 1" — three outside reviewers named that
// independently, and they were right: a dead link on /profiles or /me was
// invisible to it.
const IN_SHELL_ROUTES = [
  "/",
  "/calls/1",
  "/me",
  "/profiles",
  "/profiles/new",
  "/profiles/1",
  "/profiles/1/edit",
  "/admin",
  "/admin/users",
  "/admin/users/pending",
];
// PreAuthLayout's pages. They render inside their own <main> (PreAuthLayout.tsx)
// and make no authenticated call, so they need no session.
const PRE_AUTH_ROUTES = ["/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"];
const ALL_ROUTES = [...IN_SHELL_ROUTES, ...PRE_AUTH_ROUTES];

/**
 * Navigate, and PROVE the page rendered before the caller asserts anything.
 *
 * This is the load-bearing part of the walk. A route that throws during render
 * takes react-router's default error boundary (neither router declares an
 * errorElement), which replaces the whole subtree — so the page has no links and
 * no landmark, and every "no bad href here" assertion passes over it. Exactly one
 * <main> is both the landmark assertion and the proof of life: an error screen
 * has none, and a nested-landmark regression has two.
 */
async function gotoRendered(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(
    page.getByText("Unexpected Application Error", { exact: false }),
    `${path} crashed into react-router's error boundary`,
  ).toHaveCount(0);
  await expect(page.locator("main"), `main landmarks on ${path}`).toHaveCount(1);
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

test("no rendered link anywhere in release 1 points into the excluded half", async ({
  page,
}) => {
  // The browser-level counterpart to src/noCockpitLinks.test.tsx, which is a
  // SOURCE scan and EXEMPTs four KIND-B files whose /cockpit or /admin/* literal
  // is edition-gated at its render site (a scan cannot tell a gated literal from
  // an ungated one). This asks the rendered DOM instead, on EVERY mounted route —
  // the previous version visited three, which left the exempted files' other
  // screens uncovered.
  await mockApi(page);
  const offenders: string[] = [];
  for (const path of ALL_ROUTES) {
    await gotoRendered(page, path);
    for (const href of await hrefsOn(page)) {
      if (EXCLUDED_PREFIXES.some((p) => href.startsWith(p))) offenders.push(`${path} -> ${href}`);
    }
  }
  expect(offenders).toEqual([]);
});

test("every mounted release-1 route renders exactly one main landmark", async ({ page }) => {
  // SimpleShell shipped a <main> wrapper around <Outlet/> while all twelve
  // release-1 pages render their own, and the unit test that should have caught it
  // asserted getByRole("main") against a harness child of <p>calls page</p> — so it
  // passed only because the fake child had no landmark. The assertion lives inside
  // gotoRendered, so this test is the coverage claim: all fifteen routes, not two.
  await mockApi(page);
  for (const path of ALL_ROUTES) await gotoRendered(page, path);
  expect(ALL_ROUTES.length).toBe(15); // the count is the claim; keep it in step with the router
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

test("no critical a11y violations on the release-1 landing page", async ({ page }) => {
  // Landmarks moved to their own walk above. This is axe, and it is scoped
  // honestly: `impact === "critical"` only, matching e2e/dashboard.spec.ts. It is
  // a floor, not WCAG coverage — the contrast ratios are pinned by
  // src/theme/contrast.test.ts and the painted ground by the test below.
  await mockApi(page);
  await gotoRendered(page, "/");
  await expect(page.getByRole("listitem").first()).toBeVisible({ timeout: 10_000 });
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});

test("with no profile the landing page is an invitation, not the archive", async ({ page }) => {
  // The server still answers with calls in this state — dashboard.py's unranked
  // fallback is deliberately untouched, and the full edition keeps it — so the
  // fixture keeps CALLS' one item on purpose: the point is that the browser
  // renders NONE of them. This is the only place the branch is checked against
  // the REAL match bundle rather than a jsdom render of the component.
  await mockApi(page);
  await page.route(
    (url) => url.pathname === "/api/calls",
    (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(CALLS_NO_PROFILE),
      }),
  );
  await gotoRendered(page, "/");
  await expect(page.getByRole("heading", { name: "No research profile yet." })).toBeVisible();
  await expect(page.getByRole("link", { name: "Create a research profile" })).toHaveAttribute(
    "href",
    "/profiles/new",
  );
  // THE FEED IS GONE, and these are the assertions with teeth: a screen that
  // rendered the onboarding copy ABOVE the list would pass the two above.
  await expect(page.getByRole("link", { name: "Genomics Grant" })).toHaveCount(0);
  await expect(page.getByRole("navigation", { name: "Triage views" })).toHaveCount(0);
});

test("the account menu offers Settings as well as Sign out", async ({ page }) => {
  // /me is mounted by router.simple.tsx and the account menu is its ONLY entry
  // point in release 1 — SimpleShell's nav has Calls / Profiles / Admin and
  // nothing else. Nothing asserted this item existed, so deleting it left every
  // test green while stranding a mounted route.
  await mockApi(page);
  await gotoRendered(page, "/");
  await page.getByRole("button", { name: /account menu for p@umu\.se/i }).click();
  await page.getByRole("menuitem", { name: "Settings" }).click();
  await expect(page).toHaveURL(/\/me$/);
  await expect(page.locator("main")).toHaveCount(1);
});

// --- who can see a profile (release 1's sharing rule, in a browser) --------
//
// The backend half — that GET /api/profiles OMITS what the caller cannot read,
// and that the four sharing endpoints refuse a stranger — is pinned by
// tests/test_profile_access.py. These four scenarios are the OTHER half, and only
// a browser can answer them: that the owner's card is mounted at all, that the
// Mantine control sends the position it was moved to, that the add form uses the
// ADD endpoint rather than the REPLACE one, and that the list page renders
// exactly the profiles it was handed.

test("the owner's sharing card names who a private profile is shared with", async ({ page }) => {
  // ProfileDetail mounts the card only when the payload says can_edit, and the
  // card is the ONLY way an owner can discover a profile is private — nothing
  // else on the page says so. A card that does not mount is a profile nobody can
  // unshare.
  await mockApi(page);
  await gotoRendered(page, "/profiles/1");
  await expect(page.getByRole("heading", { name: "Who can see this" })).toBeVisible();
  await expect(page.getByRole("radio", { name: "Only me and admins" })).toBeChecked();
  // Name AND email, from GET /viewers: a list of user ids is unusable, and two
  // colleagues can share a first name.
  await expect(page.getByText("Ada Lovelace")).toBeVisible();
  await expect(page.getByText("ada@umu.se")).toBeVisible();
});

test("moving the sharing control sends the position it was moved TO, both ways", async ({
  page,
}) => {
  // BOTH directions on purpose. A card that hard-coded one value would satisfy a
  // one-way assertion while making the other position unreachable — and the
  // unreachable one here is "private", i.e. the whole feature.
  await mockApi(page);
  const bodies: unknown[] = [];
  await page.route("**/api/profiles/1/visibility", async (route) => {
    const body: unknown = route.request().postDataJSON();
    bodies.push(body);
    // Echo it: the card prefers the server's answer over the prop, so echoing is
    // what a real PUT does and keeps the second click meaningful.
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await gotoRendered(page, "/profiles/1");
  const control = page.getByRole("radiogroup", { name: "Who can see this profile" });
  // Mantine renders the radio INPUT visually hidden and puts the text in a
  // sibling <label for>; clicking the role="radio" element can miss, so click the
  // label text. This repo has hit that before.
  await control.getByText("Everyone here", { exact: true }).click();
  await expect(page.getByRole("radio", { name: "Everyone here" })).toBeChecked();
  // The control disables itself while the write is in flight, and a click on a
  // disabled label does nothing — so wait for the first write to settle rather
  // than racing the second click against it.
  await expect(page.getByRole("radio", { name: "Only me and admins" })).toBeEnabled();
  await control.getByText("Only me and admins", { exact: true }).click();
  await expect(page.getByRole("radio", { name: "Only me and admins" })).toBeChecked();
  expect(bodies).toEqual([{ visibility: "org" }, { visibility: "private" }]);
});

test("adding a viewer by email POSTs that address and lists the person back", async ({ page }) => {
  await mockApi(page);
  const writes: { method: string; body: unknown }[] = [];
  await page.route("**/api/profiles/1/viewers", async (route) => {
    const method = route.request().method();
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (method === "GET") return json(VIEWERS);
    writes.push({ method, body: route.request().postDataJSON() });
    return json({ viewers: [...VIEWERS.viewers, GRACE] });
  });
  await gotoRendered(page, "/profiles/1");
  await page.getByLabel("Share with (email)").fill(GRACE.email);
  await page.getByRole("button", { name: "Add", exact: true }).click();
  // The name comes from the RESPONSE, not from an optimistic guess: GRACE is not
  // in the mocked GET.
  await expect(page.getByText("Grace Hopper")).toBeVisible();
  // The METHOD is half the assertion. PUT /viewers is the REPLACE endpoint, so an
  // add sent there would reconcile the list DOWN to whatever it carried — the
  // rendered page would look identical here and the sharing would be wrong.
  expect(writes).toEqual([{ method: "POST", body: { email: GRACE.email } }]);
});

test("a non-viewer's profile list has no trace of the private profile", async ({ page }) => {
  await mockApi(page);
  // Registered AFTER mockApi so it wins: page.route handlers are matched in
  // REVERSE registration order, so the last one registered is tried first.
  await page.route("**/api/profiles", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        profiles: [
          {
            id: 2,
            name: "Neuro Imaging Group",
            kind: "organizational_unit",
            level: "group",
            areas_count: 1,
            budget_currency: "SEK",
            description: "MRI research.",
          },
        ],
      }),
    }),
  );
  await gotoRendered(page, "/profiles");
  // HONEST SCOPE: the API is mocked here, so this asserts that the SPA renders
  // exactly the list it was GIVEN and invents no rows of its own — NOT that the
  // server filtered anything. The server half, that GET /api/profiles omits
  // profiles the caller cannot read, is tests/test_profile_access.py gate 1.
  // The readable profile is asserted present so an empty page cannot pass the
  // absence check vacuously.
  await expect(page.getByRole("link", { name: "Neuro Imaging Group" })).toBeVisible();
  await expect(page.getByText("Digital Pathology Lab")).toHaveCount(0);
});

// --- the triage feed (spec §5), against a STATEFUL mock -------------------
//
// Everything above this line is edition SHAPE — which router compiled in, which
// links render, which palette paints. The rest is the PRODUCT: a per-profile
// inbox you empty with two keys and undo with a third.
//
// The mock below holds WHICH STATE EACH CALL IS IN, PER PROFILE, and derives
// both the list and the rail counts from it. So a rail count, a card leaving the
// inbox and the same card appearing under Saved are three consequences of one
// write, not three separately-stubbed constants.
//
// IT ALSO RECORDS THE READ PATH, and that is not decoration (T9-7). The dry run
// of this task recorded only the decision writes, so a client that dropped
// `?profile=` from the post-decision refetch — or `profile_id` from the counts
// request, which is WHOSE inbox the rail is counting — looked identical in every
// scenario. Both reads are recorded here with the two parameters that give them
// meaning, and the scenarios assert on them.
//
// Registered AFTER mockApi so it WINS (page.route matches handlers in REVERSE
// registration order) and hands back everything it does not own with
// route.fallback().
type TriageState = "inbox" | "saved" | "hidden";

// Two DIFFERENT quotes on purpose. The card renders topic_fit's quote as the
// matched sentence with no interaction at all, so a chip-expansion scenario
// written against that string would pass without the chip ever opening.
const QUOTE_TOPIC = "The call funds cancer diagnostics in clinical settings.";
const QUOTE_ELIGIBILITY = "Applicants must be established in at least two member states.";
const REASONING = "Two named partners satisfy the consortium requirement.";

// The deadline as a Stockholm calendar date N days out, computed the SAME way
// Deadline.tsx's deadlineLocalView does. "12 days left" is COMPUTED by the card
// from this string, so a hard-coded date would drift into the past and the
// assertion would quietly become a check that the call is closed.
function stockholmDatePlus(days: number): string {
  const today = new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Stockholm" });
  return new Date(new Date(`${today}T00:00:00Z`).getTime() + days * 86_400_000)
    .toISOString()
    .slice(0, 10);
}
const DAYS_OUT = 12;

// A CallOut the triage card can actually render: every field TriageCard reads,
// with the optional ones POPULATED, because the collapsed-card path is already
// covered by CALLS above and this one has to exercise programme, budget,
// days-left and chips. `deadlineDays` goes NEGATIVE for the closed-call
// scenario (D1).
function feedCall(id: number, title: string, opts: { deadlineDays?: number } = {}) {
  const local = stockholmDatePlus(opts.deadlineDays ?? DAYS_OUT);
  return {
    id,
    title,
    source_name: "Vinnova",
    source_slug: "vinnova",
    deadline: `${local}T23:59:00Z`,
    deadline_local_date: local,
    published_at: null,
    aggregate_score: 82,
    programme: "Life Science 2026",
    budget_min: 500_000,
    budget_max: 2_000_000,
    currency: "SEK",
    url: `https://example.org/calls/${id}`,
    summary: null,
    keywords: [],
    criteria: [
      {
        criterion: "topic_fit",
        label: "Topic fit",
        rating: "4",
        is_topic_fit: true,
        is_reason_code: false,
        rationale: null,
        quoted_evidence: QUOTE_TOPIC,
      },
      {
        criterion: "eligibility",
        label: "Eligibility",
        rating: "pass",
        is_topic_fit: false,
        is_reason_code: false,
        rationale: REASONING,
        quoted_evidence: QUOTE_ELIGIBILITY,
      },
    ],
    applicant_note_count: 0,
    escalated: false,
  };
}

type TriageMock = {
  /**
   * Every triage request the browser made.
   *
   *  * decisions: method, path AND query string, verbatim — `profile_id` is
   *    WHOSE decision this is, and a write that dropped it would empty the
   *    inbox on screen just as convincingly while 422-ing in production.
   *  * `GET /api/calls`, normalised to the two parameters that decide what
   *    comes back: `?profile=` and `?view=`.
   *  * `GET /api/calls/decisions/counts`, normalised to `?profile_id=`.
   */
  requests: string[];
};

async function mockTriage(
  page: Page,
  opts: {
    calls?: ReturnType<typeof feedCall>[];
    /** The effective profile when the URL carries no `?profile=`. */
    profileId?: number | null;
    profiles?: { id: number; name: string }[];
    /** Seeded decisions, keyed profile -> call -> state. */
    decided?: Record<number, Record<number, TriageState>>;
  } = {},
): Promise<TriageMock> {
  const calls = opts.calls ?? [feedCall(1, "Genomics Grant")];
  const defaultProfile = opts.profileId === undefined ? 1 : opts.profileId;
  const profiles = opts.profiles ?? [{ id: 1, name: "Group A" }];
  const requests: string[] = [];
  // PER PROFILE, because "a decision is per profile" is the rule the whole
  // table exists for: the picker scenario needs profile 2's hidden set to be a
  // different set, not the same one relabelled.
  const byProfile = new Map<number, Map<number, TriageState>>();
  const stateFor = (pid: number): Map<number, TriageState> => {
    const known = byProfile.get(pid);
    if (known) return known;
    const seeded = new Map<number, TriageState>(
      calls.map((c) => [c.id, opts.decided?.[pid]?.[c.id] ?? "inbox"]),
    );
    byProfile.set(pid, seeded);
    return seeded;
  };
  const unprocessable = (route: { fulfill: (o: object) => Promise<void> }) =>
    route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({ error: { code: "unprocessable", message: "profile_id is required" } }),
    });

  await mockApi(page);
  await page.route("**/api/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

    const decision = /^\/api\/calls\/(\d+)\/(save|hide)$/.exec(url.pathname);
    if (decision) {
      requests.push(`${req.method()} ${url.pathname}${url.search}`);
      const pid = url.searchParams.get("profile_id");
      // The real route 422s without it, and answering 200 anyway would let a
      // client that dropped the parameter pass every scenario below.
      if (pid === null) return unprocessable(route);
      const [, rawId, kind] = decision;
      // The real routes: POST /save -> saved, POST /hide -> hidden, DELETE
      // either -> undecided, and undecided IS the inbox (the absence of a row).
      const next: TriageState =
        req.method() === "DELETE" ? "inbox" : kind === "save" ? "saved" : "hidden";
      stateFor(Number(pid)).set(Number(rawId), next);
      // THE UN-DECIDED SHAPES, from web/api/calls.py: DELETE /save answers
      // CallDecisionOut{state: null} and DELETE /hide answers
      // CallHideOut{hidden: false}. The dry run's mock echoed the DECIDED
      // bodies back for both deletes, which is a wire contract no route has.
      return json(
        kind === "save"
          ? { state: next === "inbox" ? null : "saved" }
          : { hidden: next === "hidden" },
      );
    }

    if (url.pathname === "/api/calls") {
      const profileParam = url.searchParams.get("profile");
      const view = url.searchParams.get("view") ?? "archive";
      requests.push(`GET /api/calls?profile=${profileParam ?? "none"}&view=${view}`);
      const effective =
        profileParam !== null && profileParam !== "" ? Number(profileParam) : defaultProfile;
      const items = effective === null ? [] : calls.filter((c) => stateFor(effective).get(c.id) === view);
      return json({
        items,
        summary: { open_count: items.length, high_score: 0, soon_deadline: 0 },
        pagination: { page: 1, limit: 50, has_prev: false, has_next: false, total: items.length },
        ranking: { pending: 0, failed: 0 },
        effective: { profile_id: effective, source: null, sort: "newest", show_closed: false },
        facets: { sources: [], profiles },
      });
    }

    if (url.pathname === "/api/calls/decisions/counts") {
      const pid = url.searchParams.get("profile_id");
      requests.push(`GET ${url.pathname}?profile_id=${pid ?? "none"}`);
      if (pid === null) return unprocessable(route);
      const state = stateFor(Number(pid));
      const n = (s: TriageState) => [...state.values()].filter((v) => v === s).length;
      return json({ inbox: n("inbox"), saved: n("saved"), hidden: n("hidden") });
    }

    // Everything else — /api/me, /api/calls/{id}, the profile pages — is
    // mockApi's, and fallback() is what hands it back rather than answering
    // {} over it.
    return route.fallback();
  });
  return { requests };
}

const railOf = (page: Page) => page.getByRole("navigation", { name: "Triage views" });
const cardsOf = (page: Page) => page.locator("article[data-call-id]");

test("the triage feed renders the rail, its counts and the focused call's card", async ({
  page,
}) => {
  const mock = await mockTriage(page);
  await gotoRendered(page, "/");
  const rail = railOf(page);
  // aria-current is how a screen reader is told which view it is in; the rail is
  // three identical-looking links otherwise.
  await expect(rail.getByRole("link", { name: /Inbox/ })).toHaveAttribute("aria-current", "page");
  await expect(rail.getByRole("link", { name: /Saved/ })).not.toHaveAttribute(
    "aria-current",
    "page",
  );
  // FROM THE COUNTS ENDPOINT, and the fixture makes that visible: with one call
  // in the inbox, `saved` and `hidden` can only read 0 if the rail asked.
  await expect(rail.locator('[data-count="inbox"]')).toHaveText("1");
  await expect(rail.locator('[data-count="saved"]')).toHaveText("0");
  await expect(rail.locator('[data-count="hidden"]')).toHaveText("0");
  await expect(rail).toContainText("Matching against Group A");

  const card = page.locator('article[data-call-id="1"]');
  await expect(card).toHaveAttribute("data-expanded", "true");
  await expect(card.getByRole("link", { name: "Genomics Grant" })).toBeVisible();
  await expect(card.locator('[data-field="funder"]')).toHaveText("Vinnova");
  await expect(card.locator('[data-field="programme"]')).toHaveText("Life Science 2026");
  // COMPUTED, not echoed: nothing in the payload says "12 days left".
  await expect(card.locator('[data-field="days-left"]')).toContainText(`${DAYS_OUT} days left`);
  // A RANGE, because min differs from max (§5) — "500,000 SEK" would be a
  // different and wrong claim about the money.
  await expect(card.locator('[data-field="budget"]')).toHaveText("500,000 – 2,000,000 SEK");
  // The source has no description, so §5 requires a sentence and a link out
  // rather than a collapsed section.
  await expect(card.locator('[data-field="no-summary"]')).toContainText(
    "This source publishes no description with the call.",
  );
  await expect(card.locator('[data-criterion="topic_fit"]')).toHaveAttribute("data-tone", "pass");
  // BOTH READS ARE PROFILE-SCOPED (T9-7). The rail counts whose inbox? The list
  // ranks against which profile? A build that dropped either parameter renders
  // exactly this screen against the wrong data.
  expect(mock.requests).toContain("GET /api/calls?profile=none&view=inbox");
  expect(mock.requests).toContain("GET /api/calls/decisions/counts?profile_id=1");
});

test("pressing s saves the focused call, and Saved is where it lands", async ({ page }) => {
  const mock = await mockTriage(page);
  await gotoRendered(page, "/");
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  await page.keyboard.press("s");

  // THE MOVE FIRST, because it also settles the write: the rail is derived from
  // the same state the POST changed, so a count of 1 under Saved cannot be true
  // until the request landed.
  const rail = railOf(page);
  await expect(rail.locator('[data-count="saved"]')).toHaveText("1");
  await expect(rail.locator('[data-count="inbox"]')).toHaveText("0");
  // THE WRITE, exactly: endpoint AND the profile the decision belongs to. `s`
  // bound to /hide would empty the inbox just as convincingly on screen.
  expect(mock.requests.filter((r) => r.startsWith("POST "))).toEqual([
    "POST /api/calls/1/save?profile_id=1",
  ]);

  await rail.getByRole("link", { name: /Saved/ }).click();
  await expect(page).toHaveURL(/view=saved/);
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  expect(mock.requests).toContain("GET /api/calls?profile=none&view=saved");
});

test("pressing h hides the focused call, and Hidden keeps it rather than dropping it", async ({
  page,
}) => {
  const mock = await mockTriage(page);
  await gotoRendered(page, "/");
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  await page.keyboard.press("h");

  const rail = railOf(page);
  await expect(rail.locator('[data-count="hidden"]')).toHaveText("1");
  await expect(rail.locator('[data-count="inbox"]')).toHaveText("0");
  expect(mock.requests.filter((r) => r.startsWith("POST "))).toEqual([
    "POST /api/calls/1/hide?profile_id=1",
  ]);

  // "Kept here, not deleted" is the promise the Hidden empty state makes, so the
  // call has to actually be THERE.
  await rail.getByRole("link", { name: /Hidden/ }).click();
  await expect(page).toHaveURL(/view=hidden/);
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  await expect(page.locator('[data-empty="hidden"]')).toHaveCount(0);
});

test("pressing u in Hidden returns the call to the inbox, and the rail count moves with it", async ({
  page,
}) => {
  // D2. The un-decide control release 1 ships, on the path a reader actually
  // walks: they are IN Hidden looking at the call, not on its detail page after
  // hiding it twice to reveal an Undo.
  const mock = await mockTriage(page, { decided: { 1: { 1: "hidden" } } });
  await gotoRendered(page, "/?view=hidden");
  const card = page.locator('article[data-call-id="1"]');
  await expect(card).toBeVisible();
  const rail = railOf(page);
  await expect(rail.locator('[data-count="hidden"]')).toHaveText("1");
  // Keys are undiscoverable, so the same act has a control (D2).
  await expect(card.getByRole("button", { name: "Return to inbox" })).toBeVisible();

  await page.keyboard.press("u");

  // D19. THE RAIL IS CORRECT IMMEDIATELY. `useUnhideCall` invalidates BOTH
  // ["calls"] and ["decision-counts"] at hook level (invalidateDecisions); without
  // that the rail reads "Hidden 1 / Inbox 0" over a feed showing the opposite for
  // the whole of App.tsx's 20 s staleTime. The dry run of this task MEASURED that
  // stale rail and wrote it down as a live bug; it is fixed, so this asserts the
  // right number rather than describing the wrong one.
  await expect(rail.locator('[data-count="hidden"]')).toHaveText("0");
  await expect(rail.locator('[data-count="inbox"]')).toHaveText("1");
  // A DELETE OF THE ROW, never a write: undecided is the ABSENCE of a decision,
  // and a POST would re-stamp the very row this is meant to remove.
  expect(mock.requests.filter((r) => r.includes("/api/calls/1/hide"))).toEqual([
    "DELETE /api/calls/1/hide?profile_id=1",
  ]);

  // GONE from Hidden...
  await expect(page.locator('[data-empty="hidden"]')).toContainText("Nothing hidden.");
  await expect(cardsOf(page)).toHaveCount(0);
  // ...and BACK IN THE INBOX, which is the claim. A delete that only removed the
  // row would satisfy everything above.
  await rail.getByRole("link", { name: /Inbox/ }).click();
  await expect(page).toHaveURL(/view=inbox/);
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
});

test("the detail round trip lands back in the view it left", async ({ page }) => {
  // D11, and the assertion the dry run of this task got BACKWARDS: its version
  // asserted the reader lands in the INBOX after opening a hidden call, which
  // passed only because `view` was being dropped from the card's link. The URL
  // is the state, and every link preserves it.
  await mockTriage(page, { decided: { 1: { 1: "hidden" } } });
  await gotoRendered(page, "/?view=hidden");
  const card = page.locator('article[data-call-id="1"]');
  await expect(card).toBeVisible();

  await card.getByRole("link", { name: "Genomics Grant" }).click();
  // BOTH parameters travel: the profile because it decides which ranking the
  // page shows, the view because the back link has to know where to return to.
  await expect(page).toHaveURL(/\/calls\/1\?profile=1&view=hidden$/);
  await expect(page.getByRole("heading", { name: "Genomics Grant" })).toBeVisible();

  await page.getByRole("link", { name: "← All calls" }).click();
  await expect(page).toHaveURL(/\/\?profile=1&view=hidden$/);
  await expect(railOf(page).getByRole("link", { name: /Hidden/ })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  // NOT THE INBOX. With `to="/"` the reader lands on this profile's inbox, which
  // for this fixture is empty — so inbox zero is the screen a lost `view`
  // produces, and its absence is what pins the round trip.
  await expect(page.getByText("Inbox zero.")).toHaveCount(0);
});

test("the rail's profile picker switches profile, and the hidden set changes with it", async ({
  page,
}) => {
  // D11. The match index has NO other profile switcher — it left with
  // DashboardHeader in Task 7 — so without the rail's picker the product's own
  // smoke ("switching profile changes which calls are hidden") is unperformable.
  const mock = await mockTriage(page, {
    calls: [feedCall(1, "Genomics Grant"), feedCall(2, "Imaging Grant")],
    profiles: [
      { id: 1, name: "Group A" },
      { id: 2, name: "Group B" },
    ],
    decided: { 1: { 1: "hidden" }, 2: { 2: "hidden" } },
  });
  await gotoRendered(page, "/?profile=1&view=hidden");
  await expect(page.locator('article[data-call-id="1"]')).toBeVisible();
  await expect(page.locator('article[data-call-id="2"]')).toHaveCount(0);

  const pick = railOf(page).locator('[data-profile-pick="2"]');
  // THE LINK CARRIES THE VIEW. Without it the switch dumps the reader in the
  // Inbox, and "switching profile changes which calls are hidden" cannot be
  // observed at all.
  await expect(pick).toHaveAttribute("href", "/?profile=2&view=hidden");
  await pick.click();
  await expect(page).toHaveURL(/\/\?profile=2&view=hidden$/);

  // A DIFFERENT SET, not the same set relabelled.
  await expect(page.locator('article[data-call-id="2"]')).toBeVisible();
  await expect(page.locator('article[data-call-id="1"]')).toHaveCount(0);
  await expect(railOf(page)).toContainText("Matching against Group B");
  // And both reads followed the switch (T9-7).
  expect(mock.requests).toContain("GET /api/calls?profile=2&view=hidden");
  expect(mock.requests).toContain("GET /api/calls/decisions/counts?profile_id=2");
});

test("deciding the last call in the inbox reaches the inbox-zero screen", async ({ page }) => {
  // TWO calls, because "the inbox ends" is a claim about the LAST decision. With
  // one call the scenario cannot tell inbox-zero from a screen that renders the
  // empty state whenever anything is decided.
  await mockTriage(page, { calls: [feedCall(1, "Genomics Grant"), feedCall(2, "Imaging Grant")] });
  await gotoRendered(page, "/");
  await expect(cardsOf(page)).toHaveCount(2);

  await page.keyboard.press("s");
  await expect(cardsOf(page)).toHaveCount(1);
  await expect(page.locator('[data-empty="inbox"]')).toHaveCount(0);

  await page.keyboard.press("s");
  const zero = page.locator('[data-empty="inbox"]');
  await expect(zero).toBeVisible();
  await expect(zero).toContainText("Inbox zero.");
  // The profile's own name, not "your profile": the copy is per-view and
  // per-profile, and a shared string would be wrong for one of them.
  await expect(zero).toContainText("You have triaged every call matched against Group A.");
  await expect(cardsOf(page)).toHaveCount(0);
});

test("a criterion chip expands to the funder's own sentence and the model's reasoning", async ({
  page,
}) => {
  await mockTriage(page);
  await gotoRendered(page, "/");
  const chip = page.locator('button[data-criterion="eligibility"]');
  await expect(chip).toHaveAttribute("aria-expanded", "false");
  // HIDDEN UNTIL ASKED. Without this the two assertions after the click could be
  // satisfied by a card that renders every quote all the time.
  await expect(page.getByText(QUOTE_ELIGIBILITY)).toHaveCount(0);

  await chip.click();
  await expect(chip).toHaveAttribute("aria-expanded", "true");
  // A BLOCKQUOTE carrying THIS chip's quote. The card already shows topic_fit's
  // quote as the matched sentence with no interaction at all, so the two strings
  // differ on purpose — otherwise this passes without the chip opening.
  await expect(page.locator("blockquote", { hasText: QUOTE_ELIGIBILITY })).toHaveCount(1);
  await expect(page.getByText("The model's reasoning:")).toBeVisible();
  await expect(page.getByText(REASONING)).toBeVisible();
});

test("a saved call whose deadline has passed is still in Saved, marked closed", async ({ page }) => {
  // D1. Saved and Hidden list DECISIONS, so `open_expr` is not applied to them:
  // a researcher who saved a call because they intend to apply must not watch it
  // vanish exactly when they go looking for it. The server half is pinned by
  // tests/test_api_calls_feed_behavior.py; this is the browser half — the client
  // must not re-impose the filter the server deliberately dropped.
  await mockTriage(page, {
    calls: [feedCall(1, "Genomics Grant"), feedCall(2, "Closed Grant", { deadlineDays: -30 })],
    decided: { 1: { 2: "saved" } },
  });
  await gotoRendered(page, "/?view=saved");
  const card = page.locator('article[data-call-id="2"]');
  await expect(card).toBeVisible();
  await expect(card.getByRole("link", { name: "Closed Grant" })).toBeVisible();
  // MARKED closed, not silently listed as if it were still open.
  await expect(card.locator('[data-deadline-state="closed"]')).toContainText("Closed ·");
  // ...and no "-30 days left" beside it: a past deadline already reads "Closed".
  await expect(card.locator('[data-field="days-left"]')).toHaveCount(0);
  // Non-vacuous: the empty state would satisfy nothing above, but the count says
  // the server's own answer for Saved still holds one call.
  await expect(railOf(page).locator('[data-count="saved"]')).toHaveText("1");
  await expect(page.locator('[data-empty="saved"]')).toHaveCount(0);
});

test("with profiles but none picked the landing page offers the picker, not the feed", async ({
  page,
}) => {
  // The OTHER no-profile branch. "with no profile the landing page is an
  // invitation" above covers a user with no profiles at all; this one has two and
  // has selected neither, and sending that user to "Create a research profile"
  // would ask them to duplicate what they already own.
  await mockTriage(page, {
    profileId: null,
    profiles: [
      { id: 4, name: "Molecular oncology" },
      { id: 9, name: "Neuro imaging" },
    ],
  });
  await gotoRendered(page, "/");
  await expect(page.getByRole("heading", { name: "Pick a research profile." })).toBeVisible();
  await expect(page.locator("[data-profile-pick]")).toHaveCount(2);
  const pick = page.locator('[data-profile-pick="4"]');
  await expect(pick).toHaveText("Molecular oncology");
  // A PLAIN LINK carrying ?profile=, so back and reload both work — and NOT a
  // write of the user's stored default.
  await expect(pick).toHaveAttribute("href", "/?profile=4");
  await expect(page.getByRole("link", { name: "Create another profile" })).toHaveAttribute(
    "href",
    "/profiles/new",
  );
  // NEITHER of the two screens it could be confused with: not the feed (the
  // fixture still has a call), and not inbox zero, which says *you are done*
  // rather than *you have not started*.
  await expect(cardsOf(page)).toHaveCount(0);
  await expect(page.getByText("Inbox zero.")).toHaveCount(0);
});
