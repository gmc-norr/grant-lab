#!/usr/bin/env node
/**
 * Gate 1: prove the match build contains no proposal-assist module.
 *
 * Reads dist/module-manifest.json — the module graph Rollup actually built,
 * emitted by the moduleManifest() plugin in vite.config.ts — and asserts that no
 * module under an excluded directory is in it.
 *
 * Measured on this repo, 2026-09-03:
 *   full build           703 modules,  73 excluded  (the gate has teeth)
 *   match build          251 modules,   0 excluded  (clean)
 *   match + 1 leak       446 modules,  19 excluded  (caught)
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
  "/src/routes/Cockpit",
  "/src/routes/Workspace",
  "/src/routes/AdminDrafting",
  "/src/routes/AdminGoldenSet",
  "/src/routes/AdminDeletedWorkspace",
];

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
