import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { expect, it } from "vitest";

// No source file may emit an href into the routes the proposal workspace used to
// own; router.tsx no longer mounts them. A source scan because a render test
// misses links in conditional branches; a dynamically built href slips through
// (replace with an AST check if one appears). e2e/match.spec.ts asks the
// rendered DOM the same question.

const SRC = join(import.meta.dirname ?? __dirname, ".");

const EXCLUDED_PREFIXES = ["/cockpit", "/admin/drafting", "/admin/deleted-workspaces"];
const EXEMPT = [".test.tsx", ".test.ts", ".gen.ts"];

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (/\.(ts|tsx)$/.test(name)) out.push(p);
  }
  return out;
}

it("no source emits an href into the removed workspace routes", () => {
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
