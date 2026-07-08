import { expect, test, type Browser, type Page } from "@playwright/test";

// Collab two-browser e2e — Slice 3b-5d.D0 de-risk (extends the 3b-3/3b-4 harness).
//
// DEFERRED / OPT-IN: skip-gated so the default CI Playwright run stays GREEN.
// Requires the seed (scripts/collab_e2e_seed.py) + a front door + uvicorn. Two
// front-door modes, selected by scripts/collab_e2e_run.sh's E2E_SPA_MODE (this
// spec itself is mode-agnostic — it just navigates via the `baseURL` fixture,
// which playwright.config.ts's E2E_MODE switch — shared with the Plan-3
// dashboard e2e — points at the right origin):
//   dev  (default) — Vite dev (:5173) proxying uvicorn SPA_MODE=dev; the Yjs WS
//                     (ws://localhost:8000/api/ws/workspaces/<id>/<artifact>) is
//                     allowed by the dev CSP's broad "ws:" token.
//   prod (E2E_SPA_MODE=prod, 3b-5d.D4 THE FLIP) — uvicorn SPA_MODE=prod serves the
//                     built bundle directly on :8000 under the STRICT CSP, whose
//                     only WS allowance is the same-origin token derived from
//                     APP_BASE_URL (Task 10). No Vite/no proxy in this mode.
//   E2E_COLLAB_WS_ID  — seeded active workspace (collab on draft + spec; spec locked)
//   E2E_COOKIE_OWNER  — signed session cookie for the owner (writer everywhere)
//   E2E_COOKIE_MEMBER — signed session cookie for the member (draft writer; spec read-only)
//   E2E_OWNER_LABEL / E2E_MEMBER_LABEL — expected caret label text (email local part)
//
// Caret DOM (from @tiptap/extension-collaboration-caret 3.27.1):
//   span.collaboration-carets__caret  (widget) > div.collaboration-carets__label (name)

const WS = process.env.E2E_COLLAB_WS_ID;
const COOKIE_OWNER = process.env.E2E_COOKIE_OWNER ?? process.env.E2E_SESSION_COOKIE;
const COOKIE_MEMBER = process.env.E2E_COOKIE_MEMBER;
const OWNER_LABEL = process.env.E2E_OWNER_LABEL ?? "";
const MEMBER_LABEL = process.env.E2E_MEMBER_LABEL ?? "";

function sessionCookie(value: string, url: string) {
  return [{ name: "session", value, url }];
}

async function openAs(browser: Browser, cookie: string, baseURL: string, path: string) {
  const ctx = await browser.newContext();
  await ctx.addCookies(sessionCookie(cookie, baseURL));
  const page = await ctx.newPage();
  await page.goto(path);
  return { ctx, page };
}

/** Click "Save a version" and wait for the checkpoint POST to actually settle —
 * not just for the button to re-enable, which can pass before React Query flips
 * `isPending` (Codex Low). Endpoint: POST .../collab/{artifact}/checkpoint. */
async function saveVersion(page: Page) {
  const btn = page.getByRole("button", { name: /save a version/i });
  await expect(btn).toBeEnabled({ timeout: 8_000 });
  await Promise.all([
    page.waitForResponse(
      (r) => r.request().method() === "POST" && /\/collab\/[^/]+\/checkpoint\b/.test(r.url()),
    ),
    btn.click(),
  ]);
}

test.describe("collab two-browser (distinct users)", () => {
  test.skip(
    !WS || !COOKIE_OWNER || !COOKIE_MEMBER,
    "requires the 2-user seed — set E2E_COLLAB_WS_ID + E2E_COOKIE_OWNER + E2E_COOKIE_MEMBER",
  );

  // (a) Convergence + distinct-name presence + checkpoint + reload-persistence. [GO gate]
  test("owner and member converge, see each other's distinct caret, checkpoint persists", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft); // owner
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft); // member (draft writer)

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Owner types; member converges.
    await edA.click();
    await A.page.keyboard.type("hello-collab");
    await expect(edB).toContainText("hello-collab", { timeout: 8_000 });

    // Member moves its cursor too (broadcasts awareness back to the owner).
    await edB.click();
    await B.page.keyboard.type("-from-member");
    await expect(edA).toContainText("-from-member", { timeout: 8_000 });

    // Distinct-name presence: B sees the OWNER's caret, A sees the MEMBER's caret.
    const caretOnB = edB.locator(".collaboration-carets__caret .collaboration-carets__label");
    const caretOnA = edA.locator(".collaboration-carets__caret .collaboration-carets__label");
    await expect(caretOnB.first()).toBeVisible({ timeout: 8_000 });
    await expect(caretOnA.first()).toBeVisible({ timeout: 8_000 });
    const nameOnB = (await caretOnB.first().textContent())?.trim() ?? "";
    const nameOnA = (await caretOnA.first().textContent())?.trim() ?? "";
    expect(nameOnB.length).toBeGreaterThan(0);
    expect(nameOnA.length).toBeGreaterThan(0);
    // Real two-user presence => the two labels differ (not one shared owner name).
    expect(nameOnA).not.toBe(nameOnB);
    if (OWNER_LABEL) expect(nameOnB).toContain(OWNER_LABEL);
    if (MEMBER_LABEL) expect(nameOnA).toContain(MEMBER_LABEL);

    // Checkpoint (Save a version) on the owner, then reload and confirm persistence.
    await saveVersion(A.page);
    await A.page.reload();
    await expect(A.page.getByTestId("collab-editor")).toContainText("hello-collab", {
      timeout: 15_000,
    });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (b) Reconnect / re-sync after a dropped socket. [GO gate]
  test("member reconnects, re-syncs the latest doc, and its caret reappears", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    await edA.click();
    await A.page.keyboard.type("before-drop ");
    await expect(edB).toContainText("before-drop", { timeout: 8_000 });

    // Drop B's connectivity, mutate on A while B is offline, then restore B.
    await B.ctx.setOffline(true);
    await A.page.keyboard.type("after-drop");
    await B.ctx.setOffline(false);

    // B re-syncs the edit made while it was offline (provider reconnect + sync).
    await expect(edB).toContainText("after-drop", { timeout: 20_000 });
    // Presence self-heals: B's caret reappears for A within the ~15s heartbeat window.
    await edB.click();
    await B.page.keyboard.type("x");
    await expect(
      edA.locator(".collaboration-carets__caret"),
    ).toBeVisible({ timeout: 20_000 });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (c) Cite-via-room on a fresh section; the citation node converges to BOTH clients.
  //     D1a acceptance gate — proves the settled-latch fix (3b-5d.D1a) lets the live Cite
  //     flow complete end-to-end. Checkpoint + reload to a git==live base first so the
  //     D1b stale-snapshot 409 (a SEPARATE slice) does not confound this proof.
  test("owner cites a claim in the live editor; the pill converges to the member", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // git==live base so base_section_sha matches the live room (D1b handles co-edit drift).
    await saveVersion(A.page);
    await A.page.reload();
    await B.page.reload();
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Select a unique body word → Cite now ENABLES (the D1a fix) once settled.
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    // Select the chunk text, then dispatch a real mouseup so the composer's captureSelection
    // reads the selection (mirrors its unit test: getSelection + mouseUp). Synthetic
    // triple-click / drag did not register a selection reliably headless.
    await chunk.selectText();
    await chunk.dispatchEvent("mouseup");
    await composer.getByRole("button", { name: /add citation/i }).click();

    // The pill appears for the owner and converges to the member.
    await expect(edA.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });
    await expect(edB.getByTestId("citation-pill").first()).toBeVisible({ timeout: 10_000 });

    await A.ctx.close();
    await B.ctx.close();
  });

  // (d) Read-only member on the LOCKED SPEC. [strong signal, NOT a GO gate]
  //     A write leak (member mutates the locked spec) IS a punch-list defect.
  test("member is read-only on the locked spec but sees the owner's live edits", async ({
    browser,
    baseURL,
  }) => {
    const spec = `/cockpit/${WS}?tab=spec`;
    const owner = await openAs(browser, COOKIE_OWNER!, baseURL!, spec);
    const member = await openAs(browser, COOKIE_MEMBER!, baseURL!, spec);

    const edOwner = owner.page.getByTestId("collab-editor");
    const edMember = member.page.getByTestId("collab-editor");
    await expect(edOwner).toBeVisible({ timeout: 15_000 });
    await expect(edMember).toBeVisible({ timeout: 15_000 });

    // The member's editor surface is non-editable (editable: canWrite === false).
    await expect(edMember.locator(".ProseMirror")).toHaveAttribute("contenteditable", "false");

    // Owner edits the spec; the member sees it (ungated read path).
    await edOwner.click();
    await owner.page.keyboard.type("owner-spec-edit");
    await expect(edMember).toContainText("owner-spec-edit", { timeout: 8_000 });
    // Owner's caret is visible to the read-only member.
    await expect(
      edMember.locator(".collaboration-carets__caret"),
    ).toBeVisible({ timeout: 8_000 });

    // Write-leak check: the member cannot mutate the locked spec.
    await edMember.click();
    await member.page.keyboard.type("MEMBER-SHOULD-NOT-WRITE");
    await expect(edMember).not.toContainText("MEMBER-SHOULD-NOT-WRITE");

    await owner.ctx.close();
    await member.ctx.close();
  });

  // (e) Cite after a co-edit with NO save-first — the D1b acceptance gate. Both users
  //     edit the draft so the live room is ahead of git HEAD; the toolbar's git
  //     base_section_sha is now stale, but the cite still SUCCEEDS (200, not 409)
  //     because the backend relocates by anchor (3b-5d.D1b). Inverse of (c), which
  //     checkpoints to a git==live base first to AVOID this path.
  test("owner cites after both users edit without saving; the cite succeeds (no 409)", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Both append at the document end (the Aims section, which holds the citeable
    // words) so that section's live sha diverges from git — with NO "Save a version".
    await edA.click();
    await A.page.keyboard.press("Control+End");
    await A.page.keyboard.type(" Owner note.");
    await expect(edB).toContainText("Owner note", { timeout: 8_000 });
    await edB.click();
    await B.page.keyboard.press("Control+End");
    await B.page.keyboard.type(" Member note.");
    await expect(edA).toContainText("Member note", { timeout: 8_000 });

    // Reload ONLY the owner. The member stays connected, so the room never empties →
    // NO last-leave checkpoint → git HEAD stays BEHIND the live room. This gives the
    // owner a clean selection surface (a live, actively-co-edited editor does not
    // register a dblclick selection reliably) while preserving the stale-git-base
    // condition D1b must tolerate. The co-edits re-sync from the persisted room.
    await A.page.reload();
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edA).toContainText("Member note", { timeout: 15_000 });

    // Select exactly the word "medicine" WITHOUT a save-first — a clean, still-uncited
    // token before the existing citation pill (scenario (c) cited "diagnostic" here).
    // A center-dblclick on the long co-edited paragraph can land on the pill/whitespace
    // and yield no claim, so drive a precise DOM Range → mouseup, which ProseMirror maps
    // to a selection the toolbar reacts to. Pre-D1b this 409s (git base != live section).
    await edA.click();
    await A.page.evaluate(() => {
      const pm = document.querySelector('[data-testid="collab-editor"] .ProseMirror');
      if (!pm) throw new Error("editor not found");
      const walk = document.createTreeWalker(pm, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        const i = (n.textContent ?? "").indexOf("medicine");
        if (i >= 0) {
          const r = document.createRange();
          r.setStart(n, i);
          r.setEnd(n, i + "medicine".length);
          const sel = window.getSelection();
          sel?.removeAllRanges();
          sel?.addRange(r);
          pm.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
          return;
        }
      }
      throw new Error("'medicine' not found in editor");
    });
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    await chunk.selectText();
    await chunk.dispatchEvent("mouseup");

    // The D1b acceptance gate: the no-save cite must SUCCEED (200 with a real
    // ev_id/citekey), NOT 409 on the stale git base_section_sha. Asserting the POST
    // result directly is a STRONGER gate than a pill count — scenario (c) already left a
    // persisted pill in this shared room, so a pill-visibility check could pass on that
    // stale pill even if this cite 409'd (Codex). "medicine" is uncited this run, so a
    // 200 here is necessarily a fresh citation, not an idempotent no-op. Live pill
    // convergence is proven by scenario (c) (on a clean git==live base).
    const [citeResp] = await Promise.all([
      A.page.waitForResponse(
        (r) => /\/citations\b/.test(r.url()) && r.request().method() === "POST",
      ),
      composer.getByRole("button", { name: /add citation/i }).click(),
    ]);
    expect(citeResp.status()).toBe(200);
    const body = await citeResp.json();
    expect(body.ev_id).toBeTruthy();
    expect(body.citekey).toBeTruthy();

    await A.ctx.close();
    await B.ctx.close();
  });
});
