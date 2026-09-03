import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

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

function tokens(file: string): Record<string, string> {
  const css = readFileSync(join(import.meta.dirname ?? __dirname, file), "utf8");
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
  it.each([
    ["--app-text", "--app-editor", 4.5],
    ["--app-text", "--app-titlebar", 4.5],
    ["--app-muted", "--app-editor", 4.5],
    ["--app-muted-chrome", "--app-titlebar", 4.5],
    ["--app-link", "--app-editor", 4.5],
    ["--app-bad", "--app-editor", 4.5],
    ["--app-warn", "--app-editor", 4.5],
    // Tertiary text (timestamps) may sit at AA-large.
    ["--app-dim", "--app-editor", 3.0],
  ])("%s on %s clears %sx", (fg, bg, min) => {
    expect(ratio(t[fg], t[bg])).toBeGreaterThanOrEqual(min as number);
  });
});
