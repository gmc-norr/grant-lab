import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

// Dual-mode (dev Vite proxy / prod FastAPI build) smoke for the flipped `/` SPA.
//
// RUNNABLE (2026-09-03): scripts/collab_e2e_seed.py now seeds the password user
// this spec logs in as, plus a Call, and scripts/collab_e2e_run.sh takes a spec
// list — so `E2E_SPECS="e2e/collab.spec.ts e2e/dashboard.spec.ts"
// scripts/collab_e2e_run.sh` drives the whole thing. It still needs (1) `pnpm
// exec playwright install --with-deps chromium` (sudo for the OS libs), (2) a
// running Docker for the seeder's testcontainers Postgres, and (3) the dev
// (uvicorn SPA_MODE=dev + `pnpm dev`) or prod (`pnpm build` + uvicorn
// SPA_MODE=prod) front-door up — with uvicorn's APP_BASE_URL set to the origin
// the browser POSTs from (dev: http://localhost:5173; prod: http://localhost:8000),
// else the CSRF middleware 403s the login/logout POSTs. See playwright.config.ts.
// Until then this encodes the Rev-2 serving verifications as executable spec, not
// a placeholder.

// Real login through the React password form and JSON auth endpoint.
async function login(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByLabel(/email/i).fill("e2e@umu.se");
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

test("navigation to a call-detail page stays in the SPA", async ({ page }) => {
  await login(page);
  await page.goto("/");

  // The list rows do NOT link to /calls/<id> any more: since the reading-pane
  // migration a row selects into `?selected=<id>` and the pane offers "Open
  // full page ↗", which is the only /calls/<id> anchor in the app
  // (CallReadingPane.tsx). This test used to look for `a[href^="/calls/"]` on
  // the list and skip itself when it found none — so it skipped on EVERY run,
  // seeded feed or not, and the skip read as "no calls" rather than "the
  // selector is stale" (review 2026-09-02 A6 finding 6).
  const row = page.getByRole("listitem").first();
  await expect(row, "no calls in the feed — the seeder should have created one").toBeVisible({
    timeout: 10_000,
  });
  await row.getByRole("link").first().click();

  const openFull = page.getByRole("link", { name: /open full page/i });
  await expect(openFull).toBeVisible({ timeout: 10_000 });
  await openFull.click();
  await expect(page).toHaveURL(/\/calls\/\d+/);
});
