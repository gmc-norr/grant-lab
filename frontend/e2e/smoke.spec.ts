import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";

// Real-backend browser smoke: the only browser checks that reach FastAPI.
// e2e/match.spec.ts covers the screens in depth but mocks /api in-page, so it
// cannot see the serving, session and CSRF layers this file exercises.
//
// Run by scripts/e2e_run.sh, which boots scripts/e2e_seed.py (testcontainers
// Postgres + uvicorn) and, in dev mode, Vite:
//   E2E_SPA_MODE=dev  bash scripts/e2e_run.sh   # Vite proxy on :5173
//   E2E_SPA_MODE=prod bash scripts/e2e_run.sh   # packaged bundle on :8000
// It relies on the seed's password user, that user's default profile, and one
// open call; see the seed's docstring.

// Hard-coded in scripts/e2e_seed.py too. Change both or neither.
const EMAIL = "e2e@umu.se";
const PASSWORD = "correct-horse-1";
const CALL_TITLE = "Precision medicine programme";
// The runner exports the seeded call's id; a hand-started seed may not.
const CALL_URL = process.env.E2E_CALL_ID
  ? new RegExp(`/calls/${process.env.E2E_CALL_ID}(\\?|$)`)
  : /\/calls\/\d+(\?|$)/;

// Real login through the React password form and the JSON auth endpoint.
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill(EMAIL);
  await page.getByRole("textbox", { name: /password/i }).fill(PASSWORD);
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/"),
    page.getByRole("button", { name: /sign in/i }).click(),
  ]);
}

// The feed has rendered the seeded profile's inbox (not the no-profile screen,
// not "Loading…"), and the seeded call is on it.
async function seededCallInFeed(page: Page): Promise<Locator> {
  await expect(page.getByRole("heading", { name: "Your matches" })).toBeVisible({ timeout: 15_000 });
  const link = page.getByRole("link", { name: CALL_TITLE });
  await expect(link, "the seeded call is not in the feed - check scripts/e2e_seed.py").toBeVisible();
  return link;
}

test("/ renders the triage feed with no critical a11y violations", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await seededCallInFeed(page);
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});

test("an unknown path gets FastAPI's JSON 404, not index.html", async ({ page }) => {
  // dev: the Vite proxy forwards unknown paths to uvicorn (default-to-backend);
  // prod: uvicorn serves index.html only for manifest-owned routes.
  const res = await page.request.get("/made-up-page");
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"] ?? "").toContain("application/json");
});

test("signing out through the account menu lands on /login and ends the session", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await page.getByRole("button", { name: /account menu/i }).click();
  await Promise.all([
    page.waitForURL((url) => url.pathname === "/login"),
    page.getByRole("menuitem", { name: /sign out/i }).click(),
  ]);
  await expect(page).toHaveURL(/\/login$/);
  // The URL alone is not proof: a control that only navigated to /login would
  // pass it. The server must have dropped the session too.
  const me = await page.request.get("/api/me");
  expect(me.status()).toBe(401);
});

test("opening a call from the feed stays in the SPA and survives a reload", async ({ page }) => {
  await login(page);
  await page.goto("/");
  const link = await seededCallInFeed(page);
  // A marker on window survives a client-side navigation and dies with a full load.
  await page.evaluate(() => {
    (window as unknown as { __smoke?: string }).__smoke = "same-document";
  });
  await link.click();
  await expect(page).toHaveURL(CALL_URL);
  await expect(page.getByRole("heading", { level: 1, name: CALL_TITLE })).toBeVisible();
  expect(await page.evaluate(() => (window as unknown as { __smoke?: string }).__smoke)).toBe(
    "same-document",
  );
  // A hard refresh asks the SERVER for /calls/<id>: it must hand back the SPA
  // shell (the route is manifest-owned), not a 404.
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: CALL_TITLE })).toBeVisible();
});
