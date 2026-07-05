import { expect, test, type Browser, type Page } from "@playwright/test";

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

  // (c) Select-to-cite in the live editor. [strong signal, NOT a GO gate]
  //     D0 FINDING → D1: the Cite/Edit write-actions latch DISABLED. TiptapSelectionToolbar
  //     computes `settled = status==="connected" && provider.isSettled()` ONLY inside its
  //     selectionUpdate-triggered render, but that same selection fires a CollaborationCaret
  //     awareness broadcast → ws.send() → provider.bufferedAmount>0 at that instant →
  //     isSettled() false → writeReady false, with NO re-render when the buffer drains a
  //     tick later (SaveVersionButton avoids this by polling isSettled() every 200ms).
  //     The vitest toolbar tests mock isSettled()=true, so this is browser-only.
  //
  //     We assert the SPECIFIC current symptom — Ask ENABLED (anchoring works: Ask is
  //     gated only on `anchorable`) but Cite DISABLED (the settle latch) — rather than
  //     blanket-`test.fail`-ing the whole scenario, so a regression in convergence,
  //     selection, or the toolbar rendering still fails loudly (Codex Medium).
  //     WHEN D1 FIXES the settle re-render race, the `toBeDisabled` below flips → restore
  //     the full flow here: checkpoint (saveVersion) + reload BOTH to a git==live base
  //     (else the kernel 409s a stale base_section_sha — direct_cite_via_kernel validates
  //     vs the LIVE room, kernel.py:88-92), then click Cite → CitationComposer → pick the
  //     seeded reference → triple-click the chunk <pre> → Add citation → assert the
  //     citation-pill converges to BOTH A and B (the seed provisions a citeable ref).
  test("select-to-cite: write-actions latch disabled in the live editor (D1 gap)", async ({
    browser,
    baseURL,
  }) => {
    const draft = `/cockpit/${WS}`;
    const A = await openAs(browser, COOKIE_OWNER!, baseURL!, draft);
    // Second client so the awareness broadcast (the latch trigger) is realistic.
    const B = await openAs(browser, COOKIE_MEMBER!, baseURL!, draft);

    const edA = A.page.getByTestId("collab-editor");
    await expect(edA).toBeVisible({ timeout: 15_000 });
    await expect(B.page.getByTestId("collab-editor")).toBeVisible({ timeout: 15_000 });

    // Select a unique body word to anchor the claim.
    await edA.getByText("diagnostic", { exact: false }).first().dblclick();

    // Anchoring works → Ask (gated only on `anchorable`) enables.
    await expect(A.page.getByRole("button", { name: "Ask" })).toBeEnabled({ timeout: 8_000 });
    // The D1 symptom → Cite (also gated on `settled`) is stuck disabled by the
    // render-vs-awareness-send race. Flips to enabled once D1 lands (see header).
    await expect(A.page.getByRole("button", { name: "Cite" })).toBeDisabled();

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
