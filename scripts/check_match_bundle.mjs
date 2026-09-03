#!/usr/bin/env node
/**
 * Gate 1: prove the match build contains no proposal-assist module.
 *
 * Reads dist/module-manifest.json — the module graph Rollup actually built,
 * emitted by the moduleManifest() plugin in vite.config.ts — and asserts that no
 * module under an excluded directory is in it.
 *
 * Measured on this repo, 2026-09-03, against the eight-prefix list below:
 *   full build           703 modules,  81 excluded  (the gate has teeth)
 *   match build          384 modules,   0 excluded  (clean)
 *   match + 1 leak       474 modules,  19 excluded  (caught)
 *
 * The plan that specified this gate predicted 73 / 251 / 446. All three numbers
 * were re-measured here and none was smoothed over: 73 is exactly the first four
 * prefixes (49+19+2+3) — the three Admin prefixes add the other 4, so the shipped
 * list is strictly stronger; and 251 came from a prototype router, since
 * router.simple.tsx did not exist when the plan was written. 384 is 47 app
 * modules plus 337 node_modules, 259 of them @mantine/core.
 *
 * Two weaker designs were tried and rejected. Chunk-filename matching: Vite may
 * fold an excluded module into a differently-named chunk. String sentinels: a
 * planted CockpitSidebar import shipped 181 kB of cockpit code while every
 * sentinel reported clean, because a sentinel only detects leaks from the chunk
 * it was drawn from. Module ids have no such blind spot.
 *
 * Usage:
 *   pnpm -C frontend build:match && pnpm -C frontend check:bundle
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Directories the match edition must not reach. Substrings of a resolved module
// id, matched against POSIX-style paths.
const EXCLUDED = [
  "/src/cockpit/",
  "/src/collab/",
  // The CodeMirror editor stack. It is NOT reachable only through the cockpit:
  // editor/CodeMirrorEditor.tsx imports @codemirror/*, @lezer/highlight,
  // @uiw/codemirror-theme-vscode, react and ./citations — nothing under any
  // other prefix here. So one `import { CodeMirrorEditor }` in a match route
  // would ship the whole CodeMirror subgraph (12 @codemirror/* module ids in the
  // full build) and every other prefix would still report clean. That is exactly
  // the blind spot that defeated the string-sentinel design: a leak from a
  // subgraph the detector was not drawn from.
  "/src/editor/",
  "/src/routes/Cockpit",
  "/src/routes/Workspace",
  "/src/routes/AdminDrafting",
  "/src/routes/AdminGoldenSet",
  "/src/routes/AdminDeletedWorkspace",
  // The full edition's VS Code shell. NOT "/src/shell/" — the match edition
  // legitimately ships SimpleShell.tsx, SimpleShell.module.css and BrandMark.tsx
  // out of that directory, so the exclusion is per file.
  //
  // These are here because gate 4 (noCockpitLinks.test.tsx) EXEMPTS them
  // whole-file: nav-items.ts carries a live `to: "/cockpit"` that is a false
  // negative there by construction, and ActivityBar/TitleBar only read
  // `pathname === "/cockpit..."`. Without these entries, a SimpleShell that
  // one day imported ActivityBar would render that /cockpit link in a match
  // build with BOTH gates green.
  "/src/shell/ActivityBar",
  "/src/shell/AppShell",
  "/src/shell/Sidebar",
  "/src/shell/StatusBar",
  "/src/shell/TitleBar",
  "/src/shell/nav-items",
];

// DELIBERATELY NOT EXCLUDED: frontend/src/api/. It holds proposal-assist clients
// (workspace.ts, turns.ts, proposals.ts, rubric.ts, ...) beside the ones release 1
// needs (client.ts, session.ts, calls.ts, profiles.ts), so a directory prefix is
// impossible and a per-file list would rot silently as files are added. The
// residual exposure is bounded and small: these are thin fetch wrappers, so a
// leaked one costs a few hundred bytes and drags in no subgraph — unlike
// /src/editor/, which is why that one is listed and these are not.

const dist = resolve(process.argv[2] ?? "frontend/dist");
const manifest = resolve(dist, "module-manifest.json");

let modules;
try {
  modules = JSON.parse(readFileSync(manifest, "utf8"));
} catch {
  console.error(
    `ERROR: no module manifest at ${manifest}.\n` +
      "The moduleManifest() plugin in vite.config.ts did not run — did the build " +
      "complete, and is the plugin registered?",
  );
  process.exit(1);
}

if (!Array.isArray(modules) || modules.length === 0) {
  console.error(`ERROR: ${manifest} is empty. A build that bundles nothing proves nothing.`);
  process.exit(1);
}

const leaked = modules
  .map((m) => m.split("\\").join("/"))
  .filter((m) => EXCLUDED.some((x) => m.includes(x)))
  .sort();

if (leaked.length > 0) {
  console.error(`ERROR: the match bundle contains ${leaked.length} excluded module(s):`);
  for (const m of leaked.slice(0, 25)) console.error(`  ${m.split("/frontend/").pop()}`);
  if (leaked.length > 25) console.error(`  ... and ${leaked.length - 25} more`);
  console.error(
    "\nThe fix is an import, not a chunk name: something reachable from " +
      "router.simple.tsx imports one of these. Read the import chain and cut it.",
  );
  process.exit(1);
}

console.log(`match bundle clean: ${modules.length} modules, 0 under ${EXCLUDED.length} excluded paths`);
