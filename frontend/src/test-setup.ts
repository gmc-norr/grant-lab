// Vitest setup: register jest-dom matchers (toBeDisabled, toBeInTheDocument, …)
// and their `expect` type augmentation. Kept in src/ so tsconfig (include:
// ["src"]) typechecks the augmentation for all *.test.tsx files.
import "@testing-library/jest-dom/vitest";

// --- Layout APIs jsdom does not implement ---------------------------------------
// jsdom implements neither layout measurement nor matchMedia/ResizeObserver.
// These stubs return ZERO-rects / no-ops. They were written for CodeMirror and are
// kept because no test asserts pixel geometry and removing them is untested.

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

if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// Mantine's autosize <Textarea> subscribes to document.fonts ('loadingdone') to
// recompute its height once web fonts settle; jsdom doesn't implement
// FontFaceSet, so document.fonts is undefined and the subscribe throws on mount.
// Stub it with no-op listeners (height isn't asserted in tests).
if (typeof document !== "undefined" && !("fonts" in document)) {
  Object.defineProperty(document, "fonts", {
    configurable: true,
    value: {
      ready: Promise.resolve(),
      addEventListener: () => {},
      removeEventListener: () => {},
    },
  });
}
