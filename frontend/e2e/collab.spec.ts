import { expect, test, type Browser } from "@playwright/test";

// Collab two-browser e2e — Slice 3b-5d.D0 de-risk (extends the 3b-3/3b-4 harness).
//
// DEFERRED / OPT-IN: skip-gated so the default CI Playwright run stays GREEN.
// Requires the seed (scripts/collab_e2e_seed.py) + Vite dev + uvicorn (SPA_MODE=dev).
// The one-command path is scripts/collab_e2e_run.sh. Manual env:
//   E2E_COLLAB_WS_ID  — seeded active workspace (collab on draft + spec; spec locked)
//   E2E_COOKIE_OWNER  — signed session cookie for the owner (writer everywhere)
//   E2E_COOKIE_MEMBER — signed session cookie for the member (draft writer; spec read-only)
//   E2E_OWNER_LABEL / E2E_MEMBER_LABEL — expected caret label text (email local part)
// The Yjs WS is ws://localhost:8000/api/ws/workspaces/<id>/<artifact>, proxied by
// Vite; the dev CSP's "ws:" token allows it. Do NOT change prod CSP here (D4).
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
    const saveBtn = A.page.getByRole("button", { name: /save a version/i });
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await saveBtn.click();
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });

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

  // (c) Cite-via-room on a fresh section; the citation node appears on BOTH sides.
  //     [strong signal, NOT a GO gate] — the 409-after-co-edit variant (3b-5a C#3)
  //     is a KNOWN D1 deferral and is exercised as a documented observation in D0's
  //     run notes, not asserted here (it must not turn into a flaky hard failure).
  //
  //     STALE-STATE GUARD (Codex Medium): all scenarios share ONE server-side room,
  //     and the earlier scenarios leave UNCHECKPOINTED live Yjs edits. The Cite
  //     toolbar derives base_section_sha from the git-backed useDraft sections
  //     (TiptapSelectionToolbar.tsx:81), but the citation kernel validates it against
  //     the LIVE room section SHA (kernel.py:88-92, direct_cite_via_kernel). So a
  //     drifted git-vs-live section would 409 from ACCUMULATED state, not the intended
  //     fresh path — masking a real result. Serial execution alone does not fix this.
  //     Remedy: checkpoint (Save a version) THEN reload both, so git == live before we
  //     cite, giving this test a clean, self-consistent base regardless of test order.
  test("owner cites a claim; the pill converges to the member", async ({ browser, baseURL }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    const edB = B.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Reset to a consistent base: fold any prior scenario's uncheckpointed edits into
    // git, then reload so the toolbar's git sections match the live room (see guard above).
    const saveBtn = A.page.getByRole("button", { name: /save a version/i });
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await saveBtn.click();
    await expect(saveBtn).toBeEnabled({ timeout: 8_000 });
    await A.page.reload();
    await B.page.reload();
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(edB).toBeVisible({ timeout: 15_000 });

    // Select a unique word in the seeded section body to anchor the claim.
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();
    const citeBtn = A.page.getByRole("button", { name: "Cite" });
    await expect(citeBtn).toBeEnabled({ timeout: 8_000 });
    await citeBtn.click();

    const composer = A.page.getByRole("complementary", { name: /add a citation/i });
    await expect(composer).toBeVisible({ timeout: 5_000 });
    // Pick the seeded (indexed) reference.
    await composer.getByRole("button", { name: /Seed Reference on Precision Medicine/i }).click();
    // Select a supporting passage: triple-click the chunk <pre> (selects the line
    // and fires the mouseup captureSelection listens for).
    const chunk = composer.locator("pre").first();
    await expect(chunk).toBeVisible({ timeout: 5_000 });
    await chunk.click({ clickCount: 3 });
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
});
