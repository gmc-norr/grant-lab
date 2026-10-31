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
// src/router.simple.test.tsx and src/noCockpitLinks.test.tsx. /admin/golden-set
// left all three lists on 2026-09-07 — the router mounts it now, so it is in
// IN_SHELL_ROUTES below and the walk visits it like any other release-1 page.
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
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
      // Required on CallOut since the 2026-09-07 note endpoint (Task 3). This
      // file is typechecked NOWHERE — frontend/tsconfig.json includes only
      // `src` — so a missing required field here is not a build error, it is a
      // fixture that silently stops resembling the wire.
      decision_note: null,
      escalated: false,
      // Required on CallOut since the 2026-10-26 review fix (A4 finding 2).
      // Same reason as `decision_note` above: this file is typechecked nowhere,
      // so an omitted field is a fixture that silently stops resembling the
      // wire rather than a build error.
      ranking_failed: false,
      ranking_failed_at: null,
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
    // Required on CallDetailCallOut since the 2026-09-07 review fix: the page
    // seeds the note box from it, so a fixture without it stops resembling the
    // wire silently -- this file is typechecked NOWHERE (frontend/tsconfig.json
    // includes only `src`), exactly as CALLS above says.
    decision_note: null,
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
  digest_email_enabled: true,
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
// AdminGoldenSetOut. The catch-all's `{}` would take the page into react-router's
// error boundary (`data.triples.length` on undefined), which gotoRendered catches
// — so this is the difference between the walk visiting the page and the walk
// reporting "/admin/golden-set crashed".
const GOLDEN_SET = {
  triples: [
    {
      id: 1,
      call_id: 1,
      profile_id: 1,
      call_title: "Genomics Grant",
      profile_name: "Group A",
      expected_score: 30,
      note: "Wrong disease area.",
    },
  ],
  candidates: [
    {
      call_id: 2,
      profile_id: 1,
      call_title: "Materials Grant",
      profile_name: "Group A",
      aggregate_score: 74,
      embed_similarity: 0.21,
      reason: "dismissed_high_score",
      user_notes: ["Not our field at all."],
    },
  ],
};

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
    if (url.pathname === "/api/admin/golden-set") return json(GOLDEN_SET);
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
  // Mounted 2026-09-07. It is in the WALK, not merely in the subnav assertion
  // below: the subnav test reads hrefs off /admin and would pass just as well
  // against a link that resolves to the soft 404.
  "/admin/golden-set",
];
// PreAuthLayout's pages. They render inside their own <main> (PreAuthLayout.tsx)
// and make no authenticated call, so they need no session.
const PRE_AUTH_ROUTES = ["/login", "/register", "/auth/pending", "/auth/no-access", "/auth/error"];
const ALL_ROUTES = [...IN_SHELL_ROUTES, ...PRE_AUTH_ROUTES];

/**
 * Navigate, and PROVE the page rendered before the caller asserts anything.
 *
 * This is the load-bearing part of the walk. A route that throws during render
 * used to take react-router's default error boundary, which replaces the whole
 * subtree — so the page had no links and no landmark, and every "no bad href
 * here" assertion passed over it.
 *
 * SINCE 2026-10-28 both routers declare a root errorElement (review M-fe-2), and
 * that CHANGED WHAT A CRASH LOOKS LIKE: RouteError renders its own <main>, so
 * "exactly one landmark" is no longer on its own a proof of life — a crashed
 * route now satisfies it. The third assertion is what keeps this honest, and it
 * names RouteError's heading rather than react-router's, which the errorElement
 * means no member will ever see again.
 */
async function gotoRendered(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(
    page.getByText("Unexpected Application Error", { exact: false }),
    `${path} crashed into react-router's error boundary`,
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Couldn't load this page" }),
    `${path} crashed into the route error screen`,
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
  await expect(page.getByRole("link", { name: "Grant Lab" })).toBeVisible();
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
  // gotoRendered, so this test is the coverage claim: every route in ALL_ROUTES,
  // not two. The live count is the assertion below -- do not repeat it in prose.
  await mockApi(page);
  for (const path of ALL_ROUTES) await gotoRendered(page, path);
  expect(ALL_ROUTES.length).toBe(16); // the count is the claim; keep it in step with the router
});

test("the admin subnav offers only the four sections release 1 mounts", async ({ page }) => {
  await mockApi(page);
  await page.goto("/admin");
  const nav = page.getByRole("navigation", { name: "Admin sections" });
  await expect(nav).toBeVisible({ timeout: 10_000 });
  // In AdminNav.tsx's LINKS order, which puts Golden set SECOND — ahead of
  // Users, because /admin/drafting is what MATCH_EXCLUDED removes from between
  // them. Written from the array, not from what reads nicely.
  expect(await nav.locator("a[href]").evaluateAll((as) =>
    as.map((a) => a.getAttribute("href") ?? ""),
  )).toEqual(["/admin", "/admin/golden-set", "/admin/users", "/admin/users/pending"]);
});

test("an excluded route renders the in-app soft 404, not a cockpit", async ({ page }) => {
  await mockApi(page);
  await page.goto("/cockpit/new");
  await expect(page.getByRole("alert")).toContainText("Not found");
  // The shell is still there — a soft 404 inside the app, not a blank document.
  await expect(page.getByRole("link", { name: "Grant Lab" })).toBeVisible();
});

test("the light ground is actually PAINTED, not merely declared", async ({ page }) => {
  // theme.match.css was once completely inert — it defined the tokens at :root,
  // theme.css defined the same ones at :root and won on source order, and all
  // nine contrast unit tests stayed green because they scraped the FILE. Only a
  // browser can answer this. --app-editor: #faf9f7 is painted by
  // SimpleShell.module.css's .shell background.
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("link", { name: "Grant Lab" })).toBeVisible();
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
    decision_note: null, // required on CallOut since Task 3; see CALLS above
    escalated: false,
    // Required on CallOut since the 2026-10-26 review fix (A4 finding 2); see
    // CALLS above. Back-filled here too because this factory — not CALLS — is
    // what the triage-decision mocks below build their /api/calls items from,
    // and this file is typechecked nowhere.
    ranking_failed: false,
    ranking_failed_at: null,
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

test("profile setup is compact and mobile profile actions fit", async ({ page }) => {
  await mockApi(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await gotoRendered(page, "/profiles/new");
  await expect(page.getByRole("button", { name: "Create profile" })).toBeInViewport();
  await expect(page.getByLabel("Organization type")).toBeHidden();
  await page.getByText("Eligibility and funding preferences", { exact: true }).click();
  await expect(page.getByLabel("Organization type")).toBeVisible();
  await gotoRendered(page, "/profiles/1");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await expect(page.getByRole("link", { name: "Edit", exact: true })).toBeInViewport();
  await page.screenshot({ path: "test-results/match-profile-mobile.png", fullPage: true });
});

test("detail decisions, scoped sharing and deadline export are usable", async ({ page, context }) => {
  await mockApi(page);
  await page.route("**/api/calls/1?*", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...CALL_DETAIL, call: { ...CALL_DETAIL.call, decision_state: "saved", decision_note: "Discuss with the team", deadline: "2027-10-25T17:30:00+01:00", deadline_local_date: "2027-10-25" } }) }));
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await gotoRendered(page, "/calls/1?profile=1&view=saved");
  await expect(page.getByText("Saved to this profile")).toBeVisible();
  await expect(page.getByRole("button", { name: "Discuss with the team" })).toBeVisible();
  await page.getByRole("button", { name: "Copy link" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Link copied" })).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(new URL(copied).search).toBe("?profile=1");
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Add to calendar" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe("call-1-deadline.ics");
  const stream = await download.createReadStream();
  const chunks = [];
  for await (const chunk of stream!) chunks.push(chunk);
  const content = Buffer.concat(chunks).toString();
  expect(content).toContain("DTSTART:20271025T163000Z");
  expect(content).not.toContain("Discuss with the team");
  await page.screenshot({ path: "test-results/match-call-detail.png", fullPage: true });
});

test("a failed decision stays visible with an error", async ({ page }) => {
  await mockApi(page);
  await page.route("**/api/calls/1/save?*", route => route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: { code: "internal_error", message: "Service unavailable" } }) }));
  await gotoRendered(page, "/?profile=1");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Service unavailable");
  await expect(page.getByRole("link", { name: "Genomics Grant" })).toBeVisible();
});

test("search and deadline order survive the detail round trip", async ({ page }) => {
  await mockApi(page);
  await gotoRendered(page, "/?profile=1");
  await page.getByRole("searchbox", { name: "Search calls" }).fill("genomics");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByRole("combobox", { name: "Order" }).selectOption("deadline");
  await page.getByRole("link", { name: "Genomics Grant" }).click();
  await page.getByRole("link", { name: "← All calls" }).click();
  await expect(page.getByRole("searchbox", { name: "Search calls" })).toHaveValue("genomics");
  await expect(page.getByRole("combobox", { name: "Order" })).toHaveValue("deadline");
});

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
  await expect(page).toHaveURL(url => url.pathname === "/calls/1" && url.searchParams.get("profile") === "1" && url.searchParams.get("view") === "hidden");
  await expect(page.getByRole("heading", { name: "Genomics Grant" })).toBeVisible();

  await page.getByRole("link", { name: "← All calls" }).click();
  await expect(page).toHaveURL(url => url.pathname === "/" && url.searchParams.get("profile") === "1" && url.searchParams.get("view") === "hidden");
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
  // A REAL LINK carrying ?profile=, so back, reload and open-in-new-tab all
  // work. A plain click additionally stores the pick as the user's default
  // (UX pass 2026-10-31) -- pinned by the two tests after this one.
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

test("on a phone the pick-a-profile screen still shows the profiles to pick", async ({ page }) => {
  // UX pass 2026-10-31, reproduced on live data at 390x844: the phone rule
  // `.picker { display: none }` (meant for the feed's rail, which has its own
  // phone select) also hid THIS list, leaving "Create another profile" as the
  // only action. jsdom applies no CSS, so only a browser can hold this.
  await page.setViewportSize({ width: 390, height: 844 });
  await mockTriage(page, {
    profileId: null,
    profiles: [
      { id: 4, name: "Molecular oncology" },
      { id: 9, name: "Neuro imaging" },
    ],
  });
  await gotoRendered(page, "/");
  await expect(page.getByRole("heading", { name: "Pick a research profile." })).toBeVisible();
  await expect(page.locator('[data-profile-pick="4"]')).toBeVisible();
  await expect(page.locator('[data-profile-pick="9"]')).toBeVisible();
});

test("picking a profile stores it as the default, so the next visit skips this screen", async ({
  page,
}) => {
  await mockTriage(page, {
    profileId: null,
    profiles: [
      { id: 4, name: "Molecular oncology" },
      { id: 9, name: "Neuro imaging" },
    ],
  });
  const stored: unknown[] = [];
  await page.route("**/api/me/default-profile", async (route) => {
    stored.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ default_profile_id: 9 }),
    });
  });
  await gotoRendered(page, "/");
  await page.locator('[data-profile-pick="9"]').click();
  await expect(page).toHaveURL(/\/\?profile=9$/);
  expect(stored).toEqual([{ profile_id: 9 }]);
});

test("the New profile button shows its whole label beside the long intro", async ({ page }) => {
  // UX pass 2026-10-31: at 1440 wide the button rendered as "+ New profil",
  // squeezed by the paragraph in the same nowrap row.
  await mockApi(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await gotoRendered(page, "/profiles");
  const label = page.getByRole("link", { name: "+ New profile" });
  await expect(label).toBeVisible();
  const clipped = await label.evaluate((el) => {
    const inner = el.querySelector(".mantine-Button-label") ?? el;
    return inner.scrollWidth > inner.clientWidth;
  });
  expect(clipped).toBe(false);
});

// --- the corridor board, at the size it was authored for ---------------------
//
// REPRODUCED by an outside review: at 1920x1080 with realistic funding titles
// the page grew to 2292px and the entire second row sat below the screen. A
// corridor display has no scrollbar and nobody standing there to scroll it, so
// anything past 1080 does not exist. Short titles fitted, which is why every
// unit test and every earlier browser pass stayed green.
const LONG_TITLES = [
  "Utlysning: Kompetenscentrum för hållbar och resurseffektiv produktion inom life science 2026",
  "Horizon Europe Cluster 1 — Staff Exchanges for Personalised Medicine and Rare Disease Research",
  "Forskningsanslag för klinisk behandlingsforskning inom cancer och sällsynta diagnoser 2026-2029",
  "Vinnova: Avancerad digitalisering — datadrivna metoder för prevention och tidig upptäckt",
  "Formas årliga öppna utlysning för forskning och utveckling inom hållbar samhällsbyggnad",
  "EU4Health Direct Grant: Strengthening Cross-Border Cancer Screening Registries and Data Quality",
  "Cancerfondens forskningsanslag för translationell och patientnära forskning, ansökan 2026",
  "Marie Skłodowska-Curie Postdoctoral Fellowships — European Fellowships in Molecular Medicine",
];

const LONG_BOARD = {
  title: "Aktuella utlysningar",
  orientation: "landscape" as const,
  items_per_page: 8,
  max_pages: 3,
  page_seconds: 30,
  generated_at: "2026-09-07T05:00:00Z",
  items: LONG_TITLES.map((title, i) => ({
    title,
    url: `https://example.org/call-${i}`,
    source_name: "Stiftelsen for Strategisk Forskning i samverkan med Vinnova och Formas",
    // Relative, not the hard-coded "2026-10-15" this carried: BoardScreen drops
    // every card whose deadline has passed, so from 2026-10-16 the fixture
    // would have rendered an EMPTY board and every corridor test would have
    // gone red for a reason that has nothing to do with layout.
    deadline_local_date: stockholmDatePlus(30),
    budget_min: 500000,
    budget_max: 5000000,
    currency: "SEK",
    qr_modules: 25,
    qr_path: "M0 0h7v7h-7zM9 0h1v1h-1zM18 0h7v7h-7zM0 18h7v7h-7z",
  })),
};

// The landscape LONG_BOARD at one panel size, with all eight cards rendered.
async function openLongBoard(page: Page, width: number, height: number) {
  await page.setViewportSize({ width, height });
  await mockApi(page);
  await page.route("**/api/public/boards/**", async (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(LONG_BOARD),
    }),
  );
  // Not gotoRendered: the board screen is deliberately full-bleed and renders no
  // <main> landmark, which that helper asserts exactly one of.
  await page.goto("/b/corridor-slug");
  await expect(page.getByText("Aktuella utlysningar")).toBeVisible();

  const cards = page.locator('[data-field="board-card"]');
  await expect(cards).toHaveCount(8);

  // Every number measured below is a number IN A PARTICULAR FONT: 2.6vh is "the
  // largest size at which all eight of these titles fit whole" in Noto Sans and
  // in nothing else, which is why the wall bundles it (BoardScreen.fonts.css).
  // The faces load with `font-display: swap`, so the first layout can happen in
  // the kiosk's own fallback font and only then swap -- wait for them, or the
  // geometry races a woff2 fetch and intermittently reads the fallback's metrics.
  //
  // What this reads is `document.fonts`, the FontFaceSet, which enumerates the
  // @font-face rules the page DECLARED -- an installed system font of the same
  // name is never in it. That distinction is load-bearing and was MEASURED: the
  // first version of this guard used `document.fonts.check('700 28px "Noto
  // Sans"')`, and it stayed GREEN with the whole `BoardScreen.fonts.css` import
  // deleted, because this dev box has Noto Sans installed as a system font. A
  // check that cannot tell the bundled face from the box's own font is green on
  // exactly the machines where the bug does not bite.
  const faces = await page.evaluate(async () => {
    const specs = ['400 20px "Noto Sans"', '600 20px "Noto Sans"', '700 28px "Noto Sans"'];
    // The sample text reaches past basic Latin on purpose (U+0142 is in the
    // latin-ext subset, which is a separate woff2 under its own unicode-range).
    await Promise.all(specs.map((spec) => document.fonts.load(spec, "Utlysning Skłodowska åäö")));
    await document.fonts.ready;
    const mine = [...document.fonts].filter((face) => face.family === "Noto Sans");
    const weights = (fs: FontFace[]) => [...new Set(fs.map((f) => f.weight))].sort();
    return {
      declared: weights(mine),
      loaded: weights(mine.filter((face) => face.status === "loaded")),
    };
  });
  // Exactly the three weights BoardScreen.module.css sets, on both counts.
  // `declared` is every @font-face the wall ships: 700 (.title, .nearestDeadline,
  // .cardTitle), 600 (.cardDeadline), 400 (everything inheriting the default) --
  // a fourth is bundle weight that never renders, and this is where bundling a
  // font's whole nine-weight family shows up. `loaded` is the subset that
  // actually fetched, so a declared-but-broken woff2 is not mistaken for a
  // shipped one. Both lists are deliberately asserted: the first alone would
  // pass over a 404, the second alone over an unused face.
  expect(faces.declared, "the wall declares font weights it does not use").toEqual([
    "400",
    "600",
    "700",
  ]);
  expect(faces.loaded, "the wall's bundled Noto Sans faces did not load").toEqual([
    "400",
    "600",
    "700",
  ]);

  // And the titles must actually be SHAPED in that bundled face. The two
  // assertions above are about DECLARATION, and the one at the end of the 1920
  // test is about the ASK -- neither of them watches the render, and there is a
  // realistic regression that keeps both green: mangle the latin faces'
  // `unicode-range` to latin-ext's values (an easy edit to get wrong, since the
  // two blocks differ only in that one list) and nothing covers basic Latin any
  // more. `document.fonts.load('700 28px "Noto Sans"', "...Skodowska...")` is
  // then satisfied by the latin-EXT faces alone at all three weights, and the
  // computed family still says "Noto Sans" -- while every ordinary letter on the
  // wall falls through the stack to whatever the machine's own font is. On a
  // kiosk that is DejaVu Sans and the title sets 12.7% wider (MEASURED under the
  // forced-DejaVu fontconfig: DejaVu Sans x41 + Noto Sans x1 at 731.63px against
  // Noto Sans x42 at 649.00px). The geometry assertions below therefore cannot
  // be the backstop: on a box that HAS Noto Sans installed the same mangle moves
  // one 28px bold line from 738.00px to 740.08px (MEASURED), 0.3%, so nothing
  // truncates and every assertion in this file stays green while the wall is
  // broken on the kiosk it is built for.
  //
  // So ask Chromium which platform font it really used, over CDP
  // CSS.getPlatformFontsForNode. `isCustomFont` is the load-bearing half of this
  // and is not decoration: it says "this came from a downloaded @font-face", not
  // "a font of this name exists". MEASURED on this box, which has Noto Sans
  // INSTALLED, with one 28px bold line in the wall's own stack:
  //
  //                             Noto Sans installed (dev)   not installed (CI)
  //   faces as committed        Noto Sans custom=TRUE       Noto Sans custom=TRUE
  //   @font-face import gone    Noto Sans custom=false      DejaVu Sans  false
  //   unicode-range mangled     Noto Sans custom=false      DejaVu Sans  false
  //
  // `familyName` alone is therefore a HOLLOW guard on any box that has the font
  // installed -- it reads "Noto Sans" in all three rows of the left column --
  // and `familyName && isCustomFont` is true for the committed faces in BOTH
  // columns and false for every regression in BOTH. That is the whole point of
  // using it: the rest of this guard set catches the kiosk-font regression only
  // because CI's `system-ui` happens to be DejaVu, so it would quietly stop
  // working the day the CI image installs Noto Sans. This assertion does not
  // care either way.
  //
  // CDP is Chromium-only. The match project drives Chromium alone
  // (playwright.config.ts declares no `projects`, so it takes the default), so
  // this runs on every match run today; the condition is here so that adding a
  // Firefox or WebKit project degrades to the computed-family assertion in the
  // 1920 test instead of erroring inside newCDPSession.
  if (page.context().browser()?.browserType().name() === "chromium") {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    // Polled, not read once: the faces are `font-display: swap`, so even after
    // `document.fonts.ready` the re-shape lands on a later layout, and a single
    // read can catch the fallback frame.
    const shapedBy = async () => {
      const { root } = await cdp.send("DOM.getDocument", { depth: -1 });
      const { nodeIds } = await cdp.send("DOM.querySelectorAll", {
        nodeId: root.nodeId,
        selector: '[data-field="board-card"] [data-field="title"]',
      });
      const perTitle: string[] = [];
      for (const nodeId of nodeIds) {
        const { fonts } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId });
        // One title can legitimately use two faces -- "Marie Skłodowska-Curie"
        // takes U+0142 from the latin-ext woff2 and the rest from the latin one
        // -- and both report the same familyName, so collapse by name here.
        perTitle.push(
          [
            ...new Set(
              fonts.map((f) => (f.isCustomFont ? f.familyName : `${f.familyName} (not bundled)`)),
            ),
          ]
            .sort()
            .join(" + "),
        );
      }
      // One entry per DISTINCT answer, so the failure message names the font
      // that actually rendered rather than repeating it eight times.
      return [...new Set(perTitle)].sort();
    };
    await expect
      .poll(shapedBy, {
        message: "the wall's titles are not rendered in the bundled Noto Sans",
        timeout: 5_000,
      })
      .toEqual(["Noto Sans"]);
    await cdp.detach();
  }

  return cards;
}

// What a passer-by actually sees of each card's title, plus where the QR and
// the facts column landed inside the card. `h` is the VISIBLE height: the
// title's box (subpixel, so a 3.5-line slice shows as 3.5 and not as a rounded
// integer) intersected with every clipping ancestor up to and including the
// card, so a wrapper or the card's own `overflow: hidden` cannot slice the
// title behind the measurement's back. `sh` is the content height.
//
// Two different "cut" questions, because the 1px one cannot answer both:
//   cut    -- ANY content past the visible edge (> 1px). Used only to ask
//             whether that edge falls on a line boundary (`sliced`).
//   hidden -- at least HALF A LINE of content is not shown: a line was lost.
// MEASURED: a box exactly N lines tall reports scrollHeight ~0.09em past its
// bottom, because Chrome counts the font's content area and line-height 1.15
// is tighter than it -- while the descender INK ends 0.17px below the line box
// (bold, 28px, the dev box's default sans; fonts differ). So `scrollHeight > clientHeight + 1` reads every title that
// fits exactly as "truncated". The old title box filled the whole band, which
// is the only reason that test ever passed at 1920.
async function titleGeometry(cards: ReturnType<Page["locator"]>) {
  return cards.evaluateAll((els) =>
    els.map((card) => {
      const title = card.querySelector('[data-field="title"]') as HTMLElement;
      const facts = (card.querySelector('[data-field="funder"]') as HTMLElement).parentElement!;
      const qr = card.querySelector("svg") as SVGElement;
      const cs = getComputedStyle(title);
      const c = card.getBoundingClientRect();
      const box = title.getBoundingClientRect();
      let top = box.top;
      let bottom = box.bottom;
      for (let el = title.parentElement; el; el = el.parentElement) {
        // `display: contents` generates no box, so it clips nothing -- even
        // though it still reports its `overflow` and a 0x0 rect.
        const ecs = getComputedStyle(el);
        if (ecs.display !== "contents" && ecs.overflowY !== "visible") {
          const r = el.getBoundingClientRect();
          top = Math.max(top, r.top);
          bottom = Math.min(bottom, r.bottom);
        }
        if (el === card) break;
      }
      const h = Math.max(0, bottom - top);
      const lh = parseFloat(cs.lineHeight);
      const clamp = parseInt(cs.getPropertyValue("-webkit-line-clamp"), 10);
      return {
        text: title.innerText.slice(0, 32),
        h: Math.round(h * 100) / 100,
        sh: title.scrollHeight,
        lh: Math.round(lh * 100) / 100,
        lines: Math.round((h / lh) * 100) / 100,
        cut: title.scrollHeight > h + 1,
        hidden: title.scrollHeight - h > lh / 2,
        clamp: Number.isNaN(clamp) ? null : clamp,
        qrWidth: qr.getBoundingClientRect().width,
        qrInside: qr.getBoundingClientRect().bottom <= c.bottom + 0.5,
        factsInside: facts.getBoundingClientRect().bottom <= c.bottom + 0.5,
      };
    }),
  );
}

test("the corridor board fits 1920x1080 with realistic long titles", async ({ page }) => {
  const cards = await openLongBoard(page, 1920, 1080);

  // 1. The PAGE must not grow. This is the number the review measured.
  const scrollHeight = await page.evaluate(() => document.documentElement.scrollHeight);
  expect(scrollHeight, "the board grew taller than the screen it is displayed on").toBeLessThanOrEqual(1080);

  // 2. Every card must actually be ON the screen. A page that fits while a card
  //    hangs below the fold would pass the check above on its own.
  const bottoms = await cards.evaluateAll((els) =>
    els.map((el) => el.getBoundingClientRect().bottom),
  );
  expect(Math.max(...bottoms), "a card sits below the bottom of the screen").toBeLessThanOrEqual(1080);

  // 3. NOT by hiding things. The review asked for this explicitly: the fix must
  //    not be `overflow: hidden` over a card whose content still overflows, and
  //    must not shrink the QR below the 170px legibility floor a phone camera
  //    needs at arm's length.
  const qr = cards.first().locator("svg");
  const box = await qr.boundingBox();
  expect(box?.width ?? 0, "the QR was shrunk below its legibility floor").toBeGreaterThanOrEqual(170);

  // 4. NOT by clipping. The review asked for this in as many words. Beside the
  //    QR the title had ~165px; stacked it gets most of the card, and asserting
  //    the WIDTH is what separates this fix from an `overflow: hidden` over the
  //    old cramped column -- which would also have made the page fit.
  const shape = await cards.first().evaluate((el) => {
    const title = el.querySelector('[data-field="title"]') as HTMLElement;
    return { card: el.getBoundingClientRect().width, title: title.getBoundingClientRect().width };
  });
  expect(shape.title, "the title is still squeezed beside the QR").toBeGreaterThan(shape.card * 0.7);

  // 5. And every title must fit WHOLE. This is the assertion that makes the two
  //    above honest: a page can fit, and a title can be wide, while every card
  //    still shows an ellipsis. MEASURED at 2.6vh, the largest size at which all
  //    eight of these 87-95 character titles fit; 2.7-2.9vh truncates two of
  //    them and 3vh truncates five, so a future bump to the type size lands
  //    here with a count rather than silently shipping clipped titles.
  //    "Truncated" is a LOST LINE plus a slice check, not `scrollHeight >
  //    clientHeight + 1` -- see titleGeometry for the measurement that made the
  //    1px test read a title which fits exactly as truncated.
  const titles = await titleGeometry(cards);
  const clipped = titles.filter((t) => t.hidden).map((t) => t.text);
  expect(clipped, `${clipped.length} of 8 titles are truncated on the wall`).toEqual([]);
  const sliced = titles.filter((t) => t.cut && Math.abs(t.lines - Math.round(t.lines)) > 0.05);
  expect(sliced.map((t) => `${t.text}... lines=${t.lines}`), "a title is sliced mid-line").toEqual([]);

  const titleText = await cards.first().locator('[data-field="title"]').innerText();
  expect(titleText.trim().length).toBeGreaterThan(20);

  // 6. And the title must ASK for the bundled font first. Everything above is a
  //    measurement, and a measurement is only as portable as the font it was
  //    taken in: the app-wide stack (theme.css / theme.ts) leads with
  //    `-apple-system, "Segoe UI", system-ui`, and `system-ui` is not a font --
  //    it is "whatever this machine's default is". On a Linux kiosk defaulting
  //    to DejaVu Sans, 13% wider than Noto Sans, exactly three of these eight
  //    titles truncate (review 2026-10-26, A1 finding 6: REPRODUCED by running
  //    this file under a FONTCONFIG_FILE that maps sans-serif and system-ui to
  //    DejaVu Sans, which the runner itself cannot do).
  //
  //    This is the PORTABLE floor under the CDP platform-font assertion in
  //    openLongBoard, which is the authoritative one: that reads what Chromium
  //    actually shaped the titles in and so catches this regression and two more,
  //    on any box, but it needs CDP and therefore Chromium. This one is a plain
  //    getComputedStyle read that works in any browser, so it is what still
  //    holds if a Firefox or WebKit project is ever added to the match config.
  //    Deliberately kept as a duplicate for that reason -- it asserts the ASK,
  //    openLongBoard asserts the RENDER, and the render assertion is the one
  //    that cannot be satisfied by a font the kiosk merely happens to have.
  //    MEASURED, by deleting `.screen`'s font-family: on a box whose `system-ui`
  //    already resolves to Noto Sans -- this dev box does -- assertion 5 above
  //    stays green and nothing else in THIS test notices.
  const asks = await cards
    .first()
    .locator('[data-field="title"]')
    .evaluate((el) =>
      getComputedStyle(el)
        .fontFamily.split(",")[0]
        .trim()
        .replace(/^["']|["']$/g, ""),
    );
  expect(asks, "the wall is not asking for its bundled font first").toBe("Noto Sans");
});

// --- the same wall on a panel below 1080p ------------------------------------
//
// REPRODUCED by the 2026-10-06 review (M8): the only two sizes the gate above
// ever drove were 1920x1080 and the portrait 1080x1920, and below 1080p the wall
// broke in two different ways. At 1366x768 a fixed 170px QR band ate the card
// and the flexible title band collapsed to height 0 -- eight cards, no titles.
// At 1600x1000 every title was sliced through the middle of a line (3.5 lines
// visible), a cut descender rather than an ellipsis. Neither grows the page, so
// a page-height assertion alone stays green over both.
//
// The "fits whole" assertion of the 1920 test is deliberately NOT repeated: a
// smaller panel may honestly show fewer lines. What it may not do is show none,
// show a fraction of one, or truncate without saying so.
for (const [width, height] of [
  [1366, 768],
  [1600, 1000],
] as const) {
  test(`the corridor board keeps every title readable at ${width}x${height}`, async ({ page }) => {
    const cards = await openLongBoard(page, width, height);

    // 1. No scroll, in either direction: the screen IS the budget.
    const doc = await page.evaluate(() => ({
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
    }));
    expect(doc, "the board does not fit the screen it is displayed on").toEqual({ width, height });
    const bottoms = await cards.evaluateAll((els) => els.map((el) => el.getBoundingClientRect().bottom));
    expect(Math.max(...bottoms), "a card sits below the bottom of the screen").toBeLessThanOrEqual(height);

    const titles = await titleGeometry(cards);
    const show = (ts: typeof titles) =>
      ts.map((t) => `${t.text}... h=${t.h} sh=${t.sh} lh=${t.lh} lines=${t.lines} clamp=${t.clamp}`);

    // 2. Every title is THERE. The 1366x768 failure: h=0 on all eight.
    const collapsed = titles.filter((t) => t.h < 1);
    expect(show(collapsed), `${collapsed.length} of 8 titles collapsed to zero height`).toEqual([]);

    // 3. At least two WHOLE lines of every title. One line of a 90-character
    //    funding title is a programme name with the subject cut off.
    const short = titles.filter((t) => Math.floor(Math.min(t.h, t.sh) / t.lh + 0.05) < 2);
    expect(show(short), `${short.length} of 8 titles show fewer than two whole lines`).toEqual([]);

    // 4. A truncated title is cut AT a line boundary, never through one. The
    //    1600x1000 failure: lines=3.5 -- the bottom half of a line of glyphs.
    const sliced = titles.filter((t) => t.cut && Math.abs(t.lines - Math.round(t.lines)) > 0.05);
    expect(show(sliced), `${sliced.length} of 8 titles are sliced mid-line`).toEqual([]);

    // 5. ...and says so. A cut at a line boundary with no ellipsis reads as a
    //    complete (if odd) title; the line clamp's ellipsis is the VISIBLE
    //    truncation the reader can act on (the QR still resolves to the full
    //    call). That only holds if the box shows exactly `clamp` lines.
    const silent = titles.filter((t) => t.hidden && Math.round(t.lines) !== t.clamp);
    expect(show(silent), `${silent.length} of 8 titles are truncated without an ellipsis`).toEqual([]);

    // 6. NOT by pushing the facts or the QR out of the bottom of the card,
    //    where the card's own `overflow: hidden` would hide the loss from every
    //    assertion above. The QR may shrink with the panel, but not below the
    //    170px floor measured at 1080 -- as a share of the screen height,
    //    which is what a phone camera at arm's length actually sees.
    expect(titles.filter((t) => !t.qrInside).length, "a QR is clipped by its card").toBe(0);
    expect(titles.filter((t) => !t.factsInside).length, "the facts are clipped by their card").toBe(0);
    const qrFloor = (170 * height) / 1080 - 0.5;
    expect(Math.min(...titles.map((t) => t.qrWidth)), "the QR shrank below its floor").toBeGreaterThanOrEqual(qrFloor);
  });
}

test("the portrait board keeps long titles readable beside full-size QR codes", async ({ page }) => {
  await page.setViewportSize({ width: 1080, height: 1920 });
  await mockApi(page);
  await page.route("**/api/public/boards/**", route => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ ...LONG_BOARD, title: "Clinical funding opportunities", orientation: "portrait", items_per_page: 4, items: LONG_BOARD.items.slice(0, 4) }),
  }));
  await page.goto("/b/portrait-slug");
  const cards = page.locator('[data-field="board-card"]');
  await expect(cards).toHaveCount(4);
  const clipped = await cards.locator('[data-field="title"]').evaluateAll(titles => titles.filter(title => title.scrollHeight > title.clientHeight + 1).map(title => title.textContent));
  expect(clipped).toEqual([]);
  const geometry = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }));
  expect(geometry).toEqual({ width: 1080, height: 1920 });
  const qrWidths = await cards.locator("svg").evaluateAll(codes => codes.map(code => code.getBoundingClientRect().width));
  expect(Math.min(...qrWidths)).toBeGreaterThanOrEqual(170);
});

test("a bookmarked ?sort=foo&page=abc still loads the feed, in one request", async ({ page }) => {
  // Review 2026-10-26, re-verified from 10-06: `?sort=foo` sent FOUR requests
  // and dead-ended after 7.5 s, and `?page=abc` went on the wire literally as
  // `page=NaN`. A stale bookmark or a hand-edited URL is all it takes.
  await mockApi(page);
  const seen: string[] = [];
  // Registered AFTER mockApi so it WINS, and it VALIDATES like the real route
  // rather than answering 200 over anything: web/api/calls.py declares
  // `sort: Literal["newest", "deadline"]` and `page: int = Query(ge=1)`, so
  // FastAPI answers 422 for anything else. A mock that answered 200 regardless
  // would let an unsanitised client pass this scenario.
  await page.route(
    (url) => url.pathname === "/api/calls",
    async (route) => {
      const url = new URL(route.request().url());
      seen.push(url.search);
      const sort = url.searchParams.get("sort");
      const pageParam = url.searchParams.get("page");
      const refused =
        (sort !== null && sort !== "newest" && sort !== "deadline") ||
        (pageParam !== null && !/^[1-9][0-9]*$/.test(pageParam));
      if (refused) {
        return route.fulfill({
          status: 422,
          contentType: "application/json",
          body: JSON.stringify({
            error: { code: "validation_error", message: "sort: unexpected value" },
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(CALLS),
      });
    },
  );
  await gotoRendered(page, "/?sort=foo&page=abc");
  // THE FEED, not the error screen: the unknown sort degraded to the server's
  // own default instead of 422-ing the whole screen.
  await expect(page.getByRole("link", { name: "Genomics Grant" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0);
  // ONE request: nothing was refused, so nothing was retried. Four was the
  // measured number before this change.
  expect(seen).toHaveLength(1);
  // `sort` is DROPPED (the server's "newest" applies) and `page` is the integer
  // 1, not the string "NaN".
  expect(seen[0]).not.toContain("sort=");
  expect(seen[0]).toContain("page=1");
  expect(seen[0]).not.toContain("NaN");
});
