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
// defects were missing entries in this exact array: the /src/editor/ subgraph,
// and the /src/shell/ files — SIX entries, THREE of which gate 4 exempts
// whole-file (the precise breakdown is beside those entries below; an earlier
// draft of this line said "five", which matched neither number).
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
  // directory.
  //
  // THREE of them — ActivityBar, TitleBar and nav-items — carry gate 4's
  // whole-file EXEMPTions (noCockpitLinks.test.tsx:60-62), so without those a
  // SimpleShell that imported ActivityBar would render nav-items' live
  // `to: "/cockpit"` with BOTH gates green. The other three (AppShell, Sidebar,
  // StatusBar) are here for the ordinary reason that they are full-edition
  // furniture — gate 4 does not exempt them.
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

// The gate's SECOND half, added after three outside reviewers pointed out that
// EXCLUDED is a closed blocklist: it only ever knows what it was told, and this
// branch's worst bundle defect was a MISSING entry. ALLOWED_APP_DIRS is the
// inverse — every app directory in the bundle must be one release 1 declared —
// so a whole new area (src/assist/) fails until somebody decides about it. It
// lives in the same unlinted, untypechecked .mjs, so it needs the same pin: with
// it emptied, every app module becomes "undeclared" and the gate would go red for
// everyone, which is loud; but with a directory ADDED, a new area ships silently.
function arrayEntries(name: string): string[] {
  const src = readFileSync(SCRIPT, "utf8");
  const start = src.indexOf(`const ${name} = [`);
  expect(start, `check_match_bundle.mjs no longer declares \`const ${name} = [\``).toBeGreaterThan(
    -1,
  );
  const end = src.indexOf("];", start);
  return src
    .slice(start, end)
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => !l.startsWith("//") && !l.startsWith("*"))
    .flatMap((l) => [...l.matchAll(/"([^"]+)"/g)].map((m) => m[1]));
}

it("the bundle gate's ALLOWED_APP_DIRS is exactly the areas release 1 declares", () => {
  // Exact equality, not a subset: a subset check passes on a list that GREW, and
  // growing it is precisely how a new proposal-assist area would ship clean.
  expect(arrayEntries("ALLOWED_APP_DIRS").sort()).toEqual([
    "api/",
    "components/",
    "routes/",
    "shell/",
    "theme/",
  ]);
  expect(arrayEntries("ALLOWED_APP_ROOT_FILES").sort()).toEqual([
    "App.tsx",
    "main.tsx",
    "router.simple.tsx",
  ]);
});

it("the bundle gate distinguishes 'could not judge' from 'clean' and from 'leaked'", () => {
  // package_spa.sh maps the exit code to an edition and STAMPS it into the served
  // tree's edition.txt. While 1 meant both "leaked" and "no manifest", a match
  // build whose manifest failed to write was labelled `edition=full` and packaged
  // at exit 0 — the artefact lying about itself.
  const src = readFileSync(SCRIPT, "utf8");
  expect(src).toContain("const EXIT_CANNOT_JUDGE = 2");
  expect(src).toContain("process.exit(EXIT_CANNOT_JUDGE)");
  expect(src).toContain("process.exit(EXIT_LEAKED)");
  // ...and no bare numeric exit survives to blur them again.
  expect(/process\.exit\(\d/.test(src)).toBe(false);
});

it("SimpleShell is NOT excluded — the match shell must still be buildable", () => {
  // The control. A gate list of "/src/shell/" would satisfy every assertion
  // above by prefix accident while making the match build impossible, and a
  // reviewer would read the red bundle gate as a leak rather than as an
  // over-broad list.
  // `includes`, not `startsWith` — the same matcher check_match_bundle.mjs:100
  // uses. With startsWith an entry of "SimpleShell" would break every match
  // build while this control stayed green: a control weaker than the mechanism
  // it mirrors is not a control.
  const entries = excludedEntries();
  for (const shipped of ["/src/shell/SimpleShell.tsx", "/src/shell/BrandMark.tsx"]) {
    expect(entries.some((e) => shipped.includes(e))).toBe(false);
  }
});
