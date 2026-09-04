#!/usr/bin/env node
/**
 * Gate 1: prove the match build contains no proposal-assist module.
 *
 * Reads dist/module-manifest.json — the module graph Rollup actually built,
 * emitted by the moduleManifest() plugin in vite.config.ts — and asserts that no
 * module under an excluded directory is in it.
 *
 * Measured on this repo, 2026-09-03, against the EIGHT-prefix list this file
 * shipped with that day. The list below now has 14 entries — the three Admin
 * prefixes and the six per-file /src/shell/ ones were added afterwards, each for
 * a defect recorded in the run log — so these three numbers describe a strictly
 * WEAKER gate than the one running today. They are kept because they are what was
 * actually measured; the current list's own number is printed on every run
 * ("0 under 14 excluded paths"). Do not read 81/0/19 as current.
 *
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

// EXIT CODES, and the difference is load-bearing.
//   0  the bundle was read and is clean
//   1  the bundle was read and CONTAINS an excluded module — a real leak
//   2  the bundle could NOT be judged (no manifest, malformed, empty)
//
// 1 and 2 used to be the same code, and scripts/package_spa.sh maps "non-zero"
// to EDITION=full. So a match build whose manifest failed to write was labelled
// `edition=full`, packaged at exit 0, and stamped into the served tree's
// edition.txt — the artefact lying about itself, which is the one thing that
// file exists to prevent. A gate that cannot run must not be indistinguishable
// from a gate that ran and disagreed.
const EXIT_CLEAN = 0;
const EXIT_LEAKED = 1;
const EXIT_CANNOT_JUDGE = 2;

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
  process.exit(EXIT_CANNOT_JUDGE);
}

if (!Array.isArray(modules) || modules.length === 0) {
  console.error(`ERROR: ${manifest} is empty. A build that bundles nothing proves nothing.`);
  process.exit(EXIT_CANNOT_JUDGE);
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
  process.exit(EXIT_LEAKED);
}

// THE COMPLEMENT. Everything above is a BLOCKLIST of 14 known-bad prefixes, and a
// blocklist only ever knows what it was told: this branch's worst bundle defect was
// a MISSING entry (/src/editor/, 513 kB of CodeMirror shipping "clean"), and a whole
// new directory — src/assist/, say — would leak the same way with every prefix green.
//
// So the other half asks the inverse question: is every app directory in this bundle
// one release 1 is supposed to contain? A new top-level area under frontend/src/ then
// fails until somebody declares it here, which is a decision rather than an oversight.
//
// Its honest limit, stated so nobody reads more into a green run: this is
// DIRECTORY-level. A new file inside an allowed directory — routes/AssistPanel.tsx —
// is not caught by this half, and is exactly what the EXCLUDED blocklist above is for.
// The two halves cover different mistakes and neither subsumes the other.
const ALLOWED_APP_DIRS = ["api/", "components/", "routes/", "shell/", "theme/"];
const ALLOWED_APP_ROOT_FILES = ["App.tsx", "main.tsx", "router.simple.tsx"];

const appModules = modules
  .map((m) => m.split("\\").join("/"))
  .filter((m) => m.includes("/frontend/src/"))
  .map((m) => m.split("/frontend/src/").pop());

const undeclared = appModules
  .filter(
    (m) =>
      !ALLOWED_APP_DIRS.some((d) => m.startsWith(d)) && !ALLOWED_APP_ROOT_FILES.includes(m),
  )
  .sort();

if (undeclared.length > 0) {
  console.error(
    `ERROR: the match bundle contains ${undeclared.length} app module(s) from an ` +
      "area release 1 has not declared:",
  );
  for (const m of undeclared.slice(0, 25)) console.error(`  src/${m}`);
  if (undeclared.length > 25) console.error(`  ... and ${undeclared.length - 25} more`);
  console.error(
    "\nEither release 1 genuinely needs this area — add it to ALLOWED_APP_DIRS and " +
      "say why — or something reachable from router.simple.tsx imports code it should not.",
  );
  process.exit(EXIT_LEAKED);
}

// A bundle with no app modules at all satisfies "no undeclared area" vacuously, and
// would also satisfy the blocklist. Both halves are then green over nothing.
if (appModules.length === 0) {
  console.error(
    `ERROR: ${manifest} lists ${modules.length} modules but NONE under frontend/src/. ` +
      "Two empty sets are disjoint; this proves nothing about the app.",
  );
  process.exit(EXIT_CANNOT_JUDGE);
}

console.log(
  `match bundle clean: ${modules.length} modules, 0 under ${EXCLUDED.length} excluded paths, ` +
    `${appModules.length} app modules all under ${ALLOWED_APP_DIRS.length} declared areas`,
);
