import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Dual-mode (dev Vite proxy / prod FastAPI build) smoke for the flipped `/` SPA.
//
// DEFERRED LIVE RUN: authored against the real serving contract, but not run in
// this environment. Running it needs (1) `pnpm exec playwright install
// --with-deps chromium` (sudo for the OS libs), (2) a seeded ACTIVE e2e user
// matching the login() helper below PLUS at least one seeded call (the
// call-detail navigation test skips on an empty feed), (3) a running Postgres, and (4) the dev
// (uvicorn SPA_MODE=dev + `pnpm dev`) or prod (`pnpm build` + uvicorn
// SPA_MODE=prod) front-door up — with uvicorn's APP_BASE_URL set to the origin
// the browser POSTs from (dev: http://localhost:5173; prod: http://localhost:8000),
// else the CSRF middleware 403s the login/logout POSTs. See playwright.config.ts.
// Until then this encodes the Rev-2 serving verifications as executable spec, not
// a placeholder.

// Real login through the React password form and JSON auth endpoint.
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill("e2e@ki.se");
  await page.getByRole("textbox", { name: /password/i }).fill("correct-horse-1");
  await Promise.all([
    page.waitForURL("**/"),
    page.getByRole("button", { name: /sign in|log in/i }).click(),
  ]);
}

test("dashboard loads at / with no critical a11y violations", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await expect(page.getByRole("heading")).toBeVisible();
  // The profile <select> exists (the parity dashboard control); selecting is
  // best-effort (a fresh DB may have no profiles).
  await page
    .getByLabel(/profile/i)
    .selectOption({ index: 1 })
    .catch(() => {});
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.filter((v) => v.impact === "critical")).toEqual([]);
});

test("dev unknown path resolves to FastAPI's JSON 404 (not index.html / Vite HTML)", async ({
  page,
}) => {
  // The dev proxy forwards unknown paths to uvicorn (default-to-backend posture),
  // so a typo path is FastAPI's real JSON 404 envelope — never the SPA shell.
  const res = await page.request.get("/made-up-page");
  expect(res.status()).toBe(404);
  expect(res.headers()["content-type"] ?? "").toContain("application/json");
});

test("logout through the React UI lands on /login", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await Promise.all([
    page.waitForURL("**/login"),
    page.getByRole("button", { name: /sign out/i }).click(),
  ]);
  await expect(page).toHaveURL(/\/login$/);
});

test("navigation to a call-detail page stays in the SPA", async ({
  page,
}) => {
  await login(page);
  await page.goto("/");
  // This test requires at least one seeded call (see the seed prerequisite in
  // the header); skip explicitly on an empty feed rather than silently passing.
  const link = page.locator('a[href^="/calls/"]').first();
  const count = await page.locator('a[href^="/calls/"]').count();
  test.skip(count === 0, "no seeded calls in the feed; seed a call to exercise this");
  await link.click();
  await expect(page).toHaveURL(/\/calls\/\d+/);
});
