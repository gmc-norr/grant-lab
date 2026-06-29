// Vitest setup: register jest-dom matchers (toBeDisabled, toBeInTheDocument, …)
// and their `expect` type augmentation. Kept in src/ so tsconfig (include:
// ["src"]) typechecks the augmentation for all *.test.tsx files.
import "@testing-library/jest-dom/vitest";

// --- react-resizable-panels SSR false-positive ------------------------------
// The library logs `WARNING: Panel defaultSize prop recommended to avoid layout
// shift after server rendering` whenever a Panel omits defaultSize AND it thinks
// it is rendering on a server (`!isBrowser`). Vitest resolves the library's
// `node` export build (isBrowser hard-false), so the warning fires for our
// legitimate REMAINDER panels (AppShell `main`, ArtifactSectionEditor `editor`)
// — which intentionally omit defaultSize because their flexible siblings are
// conditional; giving them a fixed size would instead trip the library's real
// "Invalid layout total size" warning. jsdom is a browser-like env and this app
// is pure client-side (no SSR), so the message is a false positive here. Drop
// ONLY that exact string; every other console.warn — including the library's
// genuine "Invalid layout total size" misconfiguration warning — still surfaces.
const originalWarn = console.warn.bind(console);
console.warn = (...args: unknown[]) => {
  if (
    typeof args[0] === "string" &&
    args[0].includes("defaultSize prop recommended to avoid layout shift")
  ) {
    return;
  }
  originalWarn(...args);
};

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
