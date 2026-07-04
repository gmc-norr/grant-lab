// Cross-runtime write-path gate, run under vitest so CI includes it.
//
// The real comparison lives in the standalone Node verifier
// `scripts/verify_python_writepath.mjs` (also runnable as
// `node scripts/verify_python_writepath.mjs`). This wrapper imports its
// per-fixture comparison and asserts NO fixture mismatches — i.e. a real browser
// y-prosemirror reads every Python-`pm_json_to_ydoc`-produced ydoc identically
// to the committed `pm_json`. This is discoverable via `pnpm vitest run writepath`.
//
// vitest's config `include` is `src/**/*.{test,spec}.{ts,tsx}`, so this file must
// live under src/; esbuild resolves the `.mjs` import. jsdom provides node:fs,
// Buffer and yjs, so the verifier runs unchanged here.
import { describe, expect, it } from "vitest";

// eslint-disable-next-line @typescript-eslint/ban-ts-comment
// @ts-ignore -- untyped .mjs verifier; resolved by esbuild at test time.
import { compareAll, compareAllIncremental, loadWritepath } from "../scripts/verify_python_writepath.mjs";

describe("cross-runtime write path (Node reads Python-produced ydocs)", () => {
  it("reads back every Python-produced fixture identical to pm_json", () => {
    const entries = loadWritepath();
    expect(entries.length).toBeGreaterThan(0);
    const mismatches = compareAll(entries);
    // Report offending fixture names first so a failure is legible.
    expect(mismatches.map((m: { name: string }) => m.name)).toEqual([]);
    expect(mismatches).toEqual([]);
  });

  it("incremental Python-produced updates read back identical in y-prosemirror", () => {
    const mismatches = compareAllIncremental();
    expect(mismatches, JSON.stringify(mismatches)).toHaveLength(0);
  });
});
