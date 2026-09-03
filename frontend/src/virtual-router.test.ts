import { expect, it } from "vitest";
import { router as aliased } from "virtual:app-router";

import { APP_EDITION } from "./edition";
import { router as fullRouter } from "./router";
import { simpleRouter } from "./router.simple";

// The build-time alias is the ONLY mechanism keeping the excluded half out of a
// match bundle, and until this test nothing resolved it: App.tsx is its sole
// importer, no test imports App, and both router tests import their router by
// PATH. So a ROUTER_FILE hard-wired the wrong way passed tsc (both files export
// `router`), passed all 920 tests, and — in the full direction — passed the
// bundle gate too, which only ever inspects the match build.
//
// It also ties the two readers of VITE_APP_EDITION together: `aliased` comes
// from vite.config.ts's CONFIG-time read, `APP_EDITION` from edition.ts's
// import.meta.env read. If those ever diverge (a .env file read by one and not
// the other), this goes red instead of silently shipping a match shell on the
// full router.
it("the build-time alias resolves to the router this edition declares", () => {
  expect(aliased).toBe(APP_EDITION === "match" ? simpleRouter : fullRouter);
});
