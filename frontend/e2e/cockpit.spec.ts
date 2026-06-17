import { test } from "@playwright/test";

// SP2a-3 STUB — not yet wired to a seeded backend fixture. Enabled in SP2a-4 when
// select-to-cite lands and needs REAL caret behavior across atomic citation pills
// (jsdom cannot exercise caret geometry / keyboard navigation / focus order).
// Until then this is intentionally skipped so the Playwright suite stays green.
test.describe("cockpit source editor (deferred e2e)", () => {
  test.skip("caret skips an atomic citation pill via ArrowRight/ArrowLeft", async () => {
    // SP2a-4: open /cockpit/<seeded-ws>, focus the editor, ArrowRight across a
    // pill, assert the selection head lands on the far side of the marker bytes.
  });

  test.skip("Full-draft tab is not editable and Tab order skips pill internals", async () => {
    // SP2a-4: switch to Full draft, assert contenteditable=false + axe a11y scan.
  });
});
