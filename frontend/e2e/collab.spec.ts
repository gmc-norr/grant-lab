import { expect, test } from "@playwright/test";

// Collab round-trip e2e (Slice 3b-3, Task 6).
//
// DEFERRED LIVE RUN: authored against the real serving contract, but not run in
// this environment without manual setup. Running it needs:
//   (1) E2E_COLLAB_WS_ID — the UUID of a seeded ACTIVE workspace with collab
//       enabled on the "draft" artifact; produced by scripts/collab_e2e_seed.py.
//   (2) E2E_SESSION_COOKIE — a signed session cookie value (not including the
//       "session=" prefix) for an authenticated owner of that workspace; also
//       printed by the seed script.
//   (3) A running Postgres (testcontainers or real) with all Alembic migrations
//       applied and the workspace/user rows committed.
//   (4) uvicorn started in SPA_MODE=dev (the dev CSP emits
//       "connect-src 'self' ws: http://localhost:5173" so the Yjs WebSocket
//       from the Tiptap editor is allowed) plus `pnpm dev` (Vite on :5173),
//       with APP_BASE_URL=http://localhost:5173 so CSRF passes.
//   (5) `pnpm exec playwright install --with-deps chromium` (sudo for OS libs)
//       if running on a fresh machine; Chromium is cached at
//       ~/.cache/ms-playwright/chromium-1223 on the dev machine.
//
// The Yjs WS URL is ws://localhost:8000/api/ws/workspaces/<id>/draft and is
// proxied by the Vite dev server (vite.config.ts, added in Task 5).
// The dev CSP's "ws:" wildcard covers this; prod/disabled keep strict
// "connect-src 'self'" (no 'ws:' token). Do NOT change prod CSP here.
//
// When E2E_COLLAB_WS_ID is unset (default in CI without the seed step),
// every test in this file is skipped so the Playwright suite stays GREEN.

const COLLAB_WS_ID = process.env.E2E_COLLAB_WS_ID;
const SESSION_COOKIE = process.env.E2E_SESSION_COOKIE;

test.describe("collab editor round-trip", () => {
  test.skip(!COLLAB_WS_ID || !SESSION_COOKIE, "requires a seeded collab workspace — set E2E_COLLAB_WS_ID + E2E_SESSION_COOKIE");

  test("two clients converge; edit persists; Save a version checkpoints", async ({
    browser,
    baseURL,
  }) => {
    // Mint two authed browser contexts via the signed session cookie.
    // Both represent the same owner (one owner, two tabs) which is the
    // simplest valid convergence scenario for a round-trip gate.
    const cookieObj = [
      {
        name: "session",
        value: SESSION_COOKIE!,
        url: baseURL!,
      },
    ];

    const ctxA = await browser.newContext();
    await ctxA.addCookies(cookieObj);

    const ctxB = await browser.newContext();
    await ctxB.addCookies(cookieObj);

    const a = await ctxA.newPage();
    const b = await ctxB.newPage();

    const url = `/cockpit/${COLLAB_WS_ID}`;
    await a.goto(url);
    await b.goto(url);

    // Wait for the collab editor to be visible on both sides.
    const edA = a.getByTestId("collab-editor");
    const edB = b.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 10_000 });
    await expect(edB).toBeVisible({ timeout: 10_000 });

    // Client A types a unique string into the collab editor.
    await edA.click();
    await a.keyboard.type("hello-collab");

    // Client B should converge within 5 s (Yjs real-time sync via WS).
    await expect(edB).toContainText("hello-collab", { timeout: 5_000 });

    // Click "Save a version" on client A; wait for it to re-enable
    // (checkpoint settled = provider drained + idle).
    const saveBtn = a.getByRole("button", { name: /save a version/i });
    await expect(saveBtn).toBeEnabled({ timeout: 5_000 });
    await saveBtn.click();
    // Button goes disabled while saving; wait for it to re-enable.
    await expect(saveBtn).toBeEnabled({ timeout: 5_000 });

    // Reload client A and confirm the text survived (Yjs log persistence).
    await a.reload();
    await expect(a.getByTestId("collab-editor")).toContainText("hello-collab", {
      timeout: 10_000,
    });

    await ctxA.close();
    await ctxB.close();
  });

  // Presence / remote-caret e2e (Slice 3b-4, Task 12 Step 1 + Step 6).
  //
  // DEFERRED LIVE RUN: same environment requirements as the round-trip test
  // above (harness contention on :5173 means we do not run the Playwright
  // browser suite in CI without the seed step).  The presence transport itself
  // (awareness broadcast through the Yjs WS kernel) is already proven by the
  // Task 4 / Task 7 live interop gates on the backend.
  //
  // Caret class names confirmed from:
  //   frontend/node_modules/@tiptap/extension-collaboration-caret/src/collaboration-caret.ts
  //   version 3.27.1 — defaultRender emits:
  //     span.collaboration-carets__caret  (the caret widget)
  //       div.collaboration-carets__label (the name label text node inside it)
  // If the package is later upgraded and the class names change, this test
  // will fail on first live run — update the selectors to match the new names.
  test("client B sees a remote caret widget with the owner's name after client A focuses and types", async ({
    browser,
    baseURL,
  }) => {
    // Both contexts use the same owner cookie: two tabs, one owner.
    // CollaborationCaret will broadcast awareness for each context; each
    // remote peer's caret appears in the other's editor DOM.  With a single
    // owner the label text will be the owner's display name (name or email
    // prefix per collabColor.ts#displayName).
    const cookieObj = [
      {
        name: "session",
        value: SESSION_COOKIE!,
        url: baseURL!,
      },
    ];

    const ctxA = await browser.newContext();
    await ctxA.addCookies(cookieObj);

    const ctxB = await browser.newContext();
    await ctxB.addCookies(cookieObj);

    const a = await ctxA.newPage();
    const b = await ctxB.newPage();

    const url = `/cockpit/${COLLAB_WS_ID}`;
    await a.goto(url);
    await b.goto(url);

    // Wait for both editors to mount and the Yjs WS handshake to complete.
    const edA = a.getByTestId("collab-editor");
    const edB = b.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 10_000 });
    await expect(edB).toBeVisible({ timeout: 10_000 });

    // Client A focuses the editor and types — this moves A's cursor into the
    // document, which triggers an awareness update that propagates to B.
    await edA.click();
    await a.keyboard.type("presence-probe");

    // Client B should receive A's awareness state and render a remote caret
    // widget within 5 s (Yjs awareness is broadcast on every local state
    // change, independent of document sync).
    //
    // Selector rationale:
    //   .collaboration-carets__caret  — the outer <span> decoration widget
    //   .collaboration-carets__label  — the name <div> nested inside it
    // Both class names are emitted by the default render function in
    // @tiptap/extension-collaboration-caret 3.27.1 (verified from source).
    const remoteCaret = edB.locator(".collaboration-carets__caret");
    await expect(remoteCaret).toBeVisible({ timeout: 5_000 });

    const remoteLabel = remoteCaret.locator(".collaboration-carets__label");
    await expect(remoteLabel).toBeVisible({ timeout: 2_000 });

    // The label text should be the owner's display name (non-empty string).
    // We don't assert the exact name because the seed may use any fixture
    // user — just confirm it is not empty, i.e. the widget carries a name.
    const labelText = await remoteLabel.textContent();
    expect(labelText?.trim().length).toBeGreaterThan(0);

    await ctxA.close();
    await ctxB.close();
  });
});
