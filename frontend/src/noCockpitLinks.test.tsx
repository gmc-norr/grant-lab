import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

// Gate 4: no component reachable from the release-1 router may emit an href into
// the excluded half. The bundle gate proves the excluded CODE is absent; it says
// nothing about a link pointing at a route that is no longer mounted.
//
// EVERY excluded prefix, not only /cockpit. The first draft of this gate matched
// /cockpit alone and would have shipped a green run alongside four dead admin
// links: AdminNav.tsx:7,8,11 links to /admin/drafting, /admin/golden-set and
// /admin/deleted-workspaces, and AdminDashboard.tsx:164 links to /admin/golden-set.
// AdminNav is reached through AdminLayout, which the simple router mounts.
//
// This is a source scan rather than a render walk because rendering every
// release-1 screen with every data shape is not tractable, and a link inside a
// conditional branch would be missed by a render test. The tradeoff is real: a
// dynamically-built href (`"/" + "cockpit/new"`) slips through, and gate 1 would
// NOT catch it — such a component imports no excluded module. That gap is
// accepted because no such construction exists in this codebase today; if one
// appears, this gate is the wrong shape and an AST check should replace it.

const SRC = join(import.meta.dirname ?? __dirname, ".");

// Kept identical to router.simple.test.tsx's EXCLUDED_PREFIXES. If you add one
// there, add it here — a route the simple router stops mounting is a route
// nothing may link to.
const EXCLUDED_PREFIXES = [
  "/cockpit",
  "/admin/drafting",
  "/admin/golden-set",
  "/admin/deleted-workspaces",
];

// Files that legitimately mention /cockpit because they are the FULL edition.
const EXEMPT = [
  "router.tsx", // the full router mounts the cockpit; that is its job
  "cockpit/", // the cockpit's own source
  "collab/", // the collab editor
  "routes/Cockpit", // Cockpit.tsx, CockpitPlaceholder, etc.
  "routes/Workspace", // WorkspaceList, WorkspaceCreate
  "routes/WorkspacesFilters",
  "noCockpitLinks.test.tsx", // this file
  "router.simple.test.tsx", // names the excluded prefixes on purpose
  ".test.tsx",
  ".test.ts",

  // --- Added while turning this gate green. The brief anticipated four
  // offenders; the first red run named sixteen lines across eleven files. Each
  // addition below is one of two kinds, and NEITHER is "a link we decided to
  // live with".
  //
  // KIND A — full-edition-only source, i.e. the same category as "cockpit/",
  // "routes/Cockpit" and "routes/Workspace" above. Measured, not assumed: a
  // `pnpm build:match` on 2026-09-04 emitted 47 app modules, and NONE of these
  // files is among them (checked by name against dist/module-manifest.json).
  // Release 1 cannot render them, so a link inside them cannot be dead in
  // release 1.
  "routes/AdminDrafting", // the /admin/drafting page itself
  "routes/AdminDeletedWorkspace", // AdminDeletedWorkspaces.tsx + ...Detail.tsx
  "shell/ActivityBar", // the VS Code rail; release 1 renders SimpleShell
  "shell/TitleBar", // ditto
  "shell/nav-items", // ACTIVITY_TOP's "/cockpit" entry, read only by ActivityBar
  ".gen.ts", // routes.gen.ts (server-shared manifest) + api/schema.gen.ts (a
  // doc comment on the wire schema). Generated files: they cannot be hand-edited
  // and a gate that demands edits to them is a gate that gets disabled. Both are
  // absent from the match build.
  //
  // routes/AdminGoldenSet is the same KIND-A category but is deliberately NOT
  // listed: it emits no such literal today, and exempting a file pre-emptively
  // is how a list stops describing reality.
  //
  // KIND B — links that ARE in the release-1 graph and are gated on the edition
  // at their render site. The scan cannot tell a gated string literal from an
  // ungated one, so these two are pinned by render tests over BOTH branches
  // instead: AdminNav.test.tsx and AdminDashboard.test.tsx. Those tests are the
  // only thing holding the gating for these files — keep them that way.
  "routes/AdminNav.tsx",
  "routes/AdminDashboard.tsx",
];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

it("no release-1 source emits an href into the excluded half", () => {
  const offenders: string[] = [];
  for (const file of walk(SRC)) {
    const rel = file.slice(SRC.length + 1);
    if (EXEMPT.some((e) => rel.includes(e))) continue;
    const text = readFileSync(file, "utf8");
    text.split("\n").forEach((line, i) => {
      for (const pre of EXCLUDED_PREFIXES) {
        if (line.includes(`"${pre}`) || line.includes(`'${pre}`) || line.includes(`\`${pre}`)) {
          offenders.push(`${rel}:${i + 1}  ${line.trim()}`);
        }
      }
    });
  }
  expect(offenders).toEqual([]);
});
