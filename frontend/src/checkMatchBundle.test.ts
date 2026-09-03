import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

// Gate 1's own gate.
//
// scripts/check_match_bundle.mjs is a repo-root .mjs: it is outside
// frontend/tsconfig.json's `include: ["src"]`, outside oxlint's `src e2e` scope,
// and no test imported it. So its EXCLUDED array — the entire content of the
// bundle gate — could be emptied and EVERY gate on this branch would stay green
// while `check:bundle` printed "match bundle clean: N modules, 0 under 0
// excluded paths". That is not hypothetical: BOTH of this branch's worst bundle
// defects were missing entries in this exact array (the /src/editor/ subgraph
// and the five /src/shell/ files that gate 4 exempts whole-file).
//
// The script is PARSED, not imported: importing it executes the gate — it reads
// dist/module-manifest.json and calls process.exit — which would make this test
// depend on a build artefact and could kill the vitest worker.
const SCRIPT = join(import.meta.dirname ?? __dirname, "../../scripts/check_match_bundle.mjs");

function excludedEntries(): string[] {
  const src = readFileSync(SCRIPT, "utf8");
  const start = src.indexOf("const EXCLUDED = [");
  expect(start, "check_match_bundle.mjs no longer declares `const EXCLUDED = [`").toBeGreaterThan(
    -1,
  );
  const end = src.indexOf("];", start);
  expect(end, "unterminated EXCLUDED array").toBeGreaterThan(start);
  // One quoted entry per line, trailing comma. Comment lines are dropped FIRST:
  // the block's prose quotes paths too (`NOT "/src/shell/"`), and a naive
  // /"([^"]+)"/g over the whole block would read those as members.
  return src
    .slice(start, end)
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => !l.startsWith("//") && !l.startsWith("*"))
    .map((l) => /^"([^"]+)",$/.exec(l)?.[1])
    .filter((x): x is string => x !== undefined);
}

// The set as of this branch, written out by hand rather than read from the
// script — reading it from the script would assert a relation between the file
// and itself, which is the failure mode this test exists to close. Same reason
// router.simple.test.tsx and AdminNav.test.tsx write their lists out.
//
// This is a MINIMUM: adding an entry is always safe and keeps this green.
// Removing one is the mutation it catches.
const REQUIRED = [
  "/src/cockpit/",
  "/src/collab/",
  // Reachable WITHOUT the cockpit — one `import { CodeMirrorEditor }` in a match
  // route ships the whole CodeMirror subgraph while every other prefix reports
  // clean. See the script's own header.
  "/src/editor/",
  "/src/routes/Cockpit",
  "/src/routes/Workspace",
  "/src/routes/AdminDrafting",
  "/src/routes/AdminGoldenSet",
  "/src/routes/AdminDeletedWorkspace",
  // The six per-file /src/shell/ entries. Per-file and not "/src/shell/" because
  // the match edition legitimately ships SimpleShell + BrandMark out of that
  // directory. These carry gate 4's whole-file EXEMPTions (noCockpitLinks.test
  // .tsx:59-63), so without them a SimpleShell that imported ActivityBar would
  // render a /cockpit link with BOTH gates green.
  "/src/shell/ActivityBar",
  "/src/shell/AppShell",
  "/src/shell/Sidebar",
  "/src/shell/StatusBar",
  "/src/shell/TitleBar",
  "/src/shell/nav-items",
];

it("the bundle gate's EXCLUDED list still contains every path release 1 must not reach", () => {
  const entries = excludedEntries();
  // A parse that returned nothing would fail the membership check below anyway,
  // but say so explicitly — "empty" and "shrunk" are different bugs.
  expect(entries.length).toBeGreaterThanOrEqual(REQUIRED.length);
  expect(REQUIRED.filter((r) => !entries.includes(r))).toEqual([]);
});

it("SimpleShell is NOT excluded — the match shell must still be buildable", () => {
  // The control. A gate list of "/src/shell/" would satisfy every assertion
  // above by prefix accident while making the match build impossible, and a
  // reviewer would read the red bundle gate as a leak rather than as an
  // over-broad list.
  const entries = excludedEntries();
  for (const shipped of ["/src/shell/SimpleShell", "/src/shell/BrandMark"]) {
    expect(entries.some((e) => shipped.startsWith(e))).toBe(false);
  }
});
