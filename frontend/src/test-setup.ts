// Vitest setup: register jest-dom matchers (toBeDisabled, toBeInTheDocument, …)
// and their `expect` type augmentation. Kept in src/ so tsconfig (include:
// ["src"]) typechecks the augmentation for all *.test.tsx files.
import "@testing-library/jest-dom/vitest";

// --- CodeMirror 6 under jsdom -------------------------------------------------
// jsdom implements neither layout measurement nor matchMedia/ResizeObserver, all
// of which EditorView touches on construction + update. These return ZERO-rects /
// no-ops: enough for EditorView to instantiate and process document transactions.
// The editor tests assert state.doc + decoration ranges, never pixel geometry, so
// zero-rects are correct (not a fidelity compromise).

const zeroRect = (): DOMRect => ({
  x: 0,
  y: 0,
  width: 0,
  height: 0,
  top: 0,
  right: 0,
  bottom: 0,
  left: 0,
  toJSON() {
    return {};
  },
});

if (typeof Range !== "undefined") {
  Range.prototype.getBoundingClientRect = zeroRect;
  // getClientRects must be array-like with item(): return an empty DOMRectList-ish.
  Range.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: function* () {} }) as unknown as DOMRectList;
}

if (typeof Element !== "undefined") {
  Element.prototype.getClientRects = () =>
    ({ length: 0, item: () => null, [Symbol.iterator]: function* () {} }) as unknown as DOMRectList;
}

if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}

if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
