import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { theme } from "./theme";

// WCAG 2.1 relative luminance + contrast ratio. Hex in, ratio out.
function luminance(hex: string): number {
  const n = hex.replace("#", "");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(n.slice(i, i + 2), 16) / 255);
  const f = (c: number) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function ratio(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((m, n) => n - m);
  return (x + 0.05) / (y + 0.05);
}

function source(file: string): string {
  return readFileSync(join(import.meta.dirname ?? __dirname, file), "utf8");
}

function tokens(file: string): Record<string, string> {
  // Comments are stripped FIRST. This regex cannot tell `--app-editor: #1a1b1e`
  // in a comment from a declaration, and a comment quoting the dark palette to
  // explain a choice was written in this very file — harmless only because it
  // happened to sit after the real declaration. A prose warning is not a fix.
  const css = source(file).replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Record<string, string> = {};
  for (const m of css.matchAll(/(--app-[\w-]+):\s*(#[0-9a-fA-F]{6})/g)) out[m[1]] = m[2];
  return out;
}

describe("the match edition palette", () => {
  const t = tokens("theme.match.css");

  it("defines every surface and text token it overrides", () => {
    for (const k of [
      "--app-editor",
      "--app-titlebar",
      "--app-sidebar",
      "--app-text",
      "--app-muted",
      "--app-muted-chrome",
      "--app-dim",
      "--app-border",
      "--app-hover",
      "--app-link",
      "--app-bad",
      "--app-warn",
    ]) {
      expect(t[k], `${k} missing from theme.match.css`).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  // AA body text on the two surfaces people actually read on.
  it("is declared at :root:root, the only reason it beats theme.css", () => {
    // THE highest-blast-radius invariant in this file, and it had no gate: with
    // a bare `:root` every assertion below still passes and the match edition
    // renders the DARK palette, because theme.css declares the same tokens at
    // equal specificity and main.tsx emits it LAST (it imports ./App before
    // ./theme/theme.css). Measured in a real match build before the fix: the
    // light --app-editor at byte 254861, the dark one after it. 0-2-0 beats
    // 0-1-0 regardless of order, which is why the selector is doubled.
    const css = source("theme.match.css").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).toContain(":root:root {");
    expect(css).not.toMatch(/^\s*:root\s*\{/m);
  });

  it.each([
    ["--app-text", "--app-editor", 4.5],
    ["--app-text", "--app-titlebar", 4.5],
    ["--app-muted", "--app-editor", 4.5],
    ["--app-muted-chrome", "--app-titlebar", 4.5],
    ["--app-link", "--app-editor", 4.5],
    ["--app-bad", "--app-editor", 4.5],
    ["--app-warn", "--app-editor", 4.5],
    // The score pill paints --app-good and --app-dim on --app-hover, not on the
    // editor ground (MatchBadge.module.css). --app-hover is LIGHTER than the
    // ground on dark and DARKER on light, so a pair that passes on the ground
    // can still fail on the chip.
    ["--app-good", "--app-hover", 4.5],
    // Tertiary text (timestamps) may sit at AA-large.
    ["--app-dim", "--app-editor", 3.0],
  ])("%s on %s clears %sx", (fg, bg, min) => {
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(min as number);
  });

  it("keeps the Mantine link colour and --app-link the same value", () => {
    // The bridge line `--mantine-color-anchor: var(--app-link)` is INERT —
    // Mantine re-declares that variable at runtime at equal specificity but
    // later in document order (see the comment in theme.match.css). The rendered
    // link colour therefore comes from theme.ts's teal[primaryShade.light].
    //
    // There is no visible defect only because the two happen to be the same
    // hex. This is what makes that a fact rather than a coincidence: edit
    // --app-link alone and the ratio assertions above move while the pixel does
    // not, which is the exact failure this file exists to prevent.
    const shade = (theme.primaryShade as { light: number }).light;
    const rendered = theme.colors!.teal![shade];
    expect(rendered.toLowerCase()).toBe(t["--app-link"].toLowerCase());
    expect(rendered.toLowerCase()).toBe(t["--app-accent"].toLowerCase());
  });
});
