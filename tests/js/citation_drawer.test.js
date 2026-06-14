"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "../../src/call_manager/web/static/app.js");

function load() {
  const handlers = {};
  const ctx = {
    document: {
      body: {
        addEventListener(n, fn) { (handlers[n] = handlers[n] || []).push(fn); },
        classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);} },
      },
      addEventListener() {},
      readyState: "complete",
      getElementById() { return null; },
      querySelector() { return null; },
      querySelectorAll() { return []; },
    },
    window: { matchMedia: () => ({ matches: ctx._overlay }), htmx: { ajax() {} } },
    setTimeout: (fn) => fn(),
    _overlay: false,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), ctx);
  return { handlers, ctx };
}

test("citeReaderUrl recognizes only the reader-route pattern on a .cite link", () => {
  const { ctx } = load();
  assert.strictEqual(
    ctx.citeReaderUrl({ getAttribute: () => "/workspaces/W/citations/ev-abc123/reader", classList: { contains: () => true } }),
    "/workspaces/W/citations/ev-abc123/reader"
  );
  // wrong href shape → null
  assert.strictEqual(
    ctx.citeReaderUrl({ getAttribute: () => "/evil", classList: { contains: () => true } }),
    null
  );
  // not a .cite link → null
  assert.strictEqual(
    ctx.citeReaderUrl({ getAttribute: () => "/workspaces/W/citations/ev-abc123/reader", classList: { contains: () => false } }),
    null
  );
});

test("readerIsOverlay reflects the narrow media query", () => {
  const { ctx } = load();
  ctx._overlay = false;
  assert.strictEqual(ctx.readerIsOverlay(), false);
  ctx._overlay = true;
  assert.strictEqual(ctx.readerIsOverlay(), true);
});

test("applyReaderModal locks scroll only in overlay mode", () => {
  const { ctx } = load();
  ctx._overlay = false;
  ctx.applyReaderModal(true);
  assert.ok(!ctx.document.body.classList.contains("reader-scroll-lock"), "desktop: no lock");
  ctx._overlay = true;
  ctx.applyReaderModal(true);
  assert.ok(ctx.document.body.classList.contains("reader-scroll-lock"), "overlay: locked");
  ctx.applyReaderModal(false);
  assert.ok(!ctx.document.body.classList.contains("reader-scroll-lock"), "cleared on close");
});

// --- Focus trap (narrow overlay / modal only) -----------------------------

function focusable() {
  return { _f: 0, focus() { this._f++; }, focused() { return this._f > 0; } };
}

// Builds a context whose #reference-drawer is a real-ish element with the given
// focusable items, hidden flag, and activeElement, under a chosen media width.
function loadWithDrawer(opts) {
  opts = opts || {};
  const items = opts.items || [];
  const handlers = {};
  const drawer = {
    id: "reference-drawer",
    hidden: opts.hidden === undefined ? false : opts.hidden,
    _focused: 0,
    focus() { this._focused++; },
    contains(el) { return items.indexOf(el) !== -1; },
    querySelectorAll() { return items.slice(); },
    querySelector() { return opts.closeBtn || null; },
    setAttribute() {},
    _returnFocusTo: null,
  };
  const ctx = {
    document: {
      body: {
        addEventListener(n, fn) { (handlers[n] = handlers[n] || []).push(fn); },
        classList: { _s: new Set(), add(c){this._s.add(c);}, remove(c){this._s.delete(c);}, contains(c){return this._s.has(c);} },
      },
      addEventListener() {},
      readyState: "complete",
      getElementById(id) {
        if (id === "reference-drawer") return drawer;
        if (id === "ev-target") return opts.mark || null;
        return null;
      },
      querySelector() { return null; },
      querySelectorAll() { return []; },
      activeElement: opts.active || null,
    },
    window: { matchMedia: () => ({ matches: ctx._overlay }), htmx: { ajax() {} } },
    setTimeout: (fn) => fn(),
    _overlay: opts.overlay === undefined ? true : opts.overlay,
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), ctx);
  return { ctx, drawer, handlers, items };
}

function tabEvt(shiftKey) {
  return { key: "Tab", shiftKey: !!shiftKey, _pd: false, preventDefault() { this._pd = true; } };
}

test("trapReaderFocus wraps forward from the last item to the first", () => {
  const a = focusable(), b = focusable();
  const { ctx } = loadWithDrawer({ items: [a, b], active: b, overlay: true });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(evt._pd, "default prevented at the boundary");
  assert.ok(a.focused(), "first item focused");
  assert.ok(!b.focused(), "last item not re-focused");
});

test("trapReaderFocus wraps backward from the first item to the last", () => {
  const a = focusable(), b = focusable();
  const { ctx } = loadWithDrawer({ items: [a, b], active: a, overlay: true });
  const evt = tabEvt(true);
  ctx.trapReaderFocus(evt);
  assert.ok(evt._pd, "default prevented at the boundary");
  assert.ok(b.focused(), "last item focused");
});

test("trapReaderFocus lets Tab flow through in the middle of the list", () => {
  const a = focusable(), b = focusable(), c = focusable();
  const { ctx } = loadWithDrawer({ items: [a, b, c], active: b, overlay: true });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(!evt._pd, "no wrap in the middle");
  assert.ok(!a.focused() && !c.focused(), "no item force-focused");
});

test("trapReaderFocus pulls focus back inside when it has escaped the drawer", () => {
  const a = focusable(), b = focusable();
  const stray = focusable(); // not in the drawer
  const { ctx } = loadWithDrawer({ items: [a, b], active: stray, overlay: true });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(evt._pd, "default prevented");
  assert.ok(a.focused(), "focus pulled to the first item");
});

test("trapReaderFocus is inert on the desktop side-panel (not overlay)", () => {
  const a = focusable(), b = focusable();
  const { ctx } = loadWithDrawer({ items: [a, b], active: b, overlay: false });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(!evt._pd, "desktop: Tab is not trapped");
  assert.ok(!a.focused(), "desktop: no wrap");
});

test("trapReaderFocus is inert when the drawer is hidden", () => {
  const a = focusable();
  const { ctx } = loadWithDrawer({ items: [a], active: a, hidden: true, overlay: true });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(!evt._pd, "hidden drawer: not trapped");
});

test("trapReaderFocus falls back to the drawer itself when nothing is focusable", () => {
  const { ctx, drawer } = loadWithDrawer({ items: [], active: null, overlay: true });
  const evt = tabEvt(false);
  ctx.trapReaderFocus(evt);
  assert.ok(evt._pd, "default prevented");
  assert.ok(drawer._focused > 0, "drawer container received focus");
});

test("keydown routes Tab to the trap and Escape still closes", () => {
  const a = focusable(), b = focusable();
  const { drawer, handlers } = loadWithDrawer({ items: [a, b], active: b, overlay: true });
  const onKey = handlers.keydown[0];
  // Tab at the last item wraps to the first
  onKey(tabEvt(false));
  assert.ok(a.focused(), "Tab routed to trap (wrapped to first)");
  // Escape closes the drawer
  onKey({ key: "Escape" });
  assert.ok(drawer.hidden, "Escape closed the drawer");
});

// --- Scroll-to-evidence on async swap (the open is racy; the swap is not) ----

function mark() {
  return { _s: 0, scrollIntoView() { this._s++; } };
}

test("htmx:afterSettle on the reader drawer scrolls the evidence into view", () => {
  const m = mark();
  const { drawer, handlers } = loadWithDrawer({ mark: m, overlay: false });
  handlers["htmx:afterSettle"][0]({ detail: { target: drawer } });
  assert.strictEqual(m._s, 1, "evidence scrolled into view after the fragment settled");
});

test("htmx:afterSettle ignores swaps into other targets", () => {
  const m = mark();
  const { handlers } = loadWithDrawer({ mark: m, overlay: false });
  handlers["htmx:afterSettle"][0]({ detail: { target: { id: "artifact-rendered" } } });
  assert.strictEqual(m._s, 0, "unrelated swap does not move the reader");
});

test("htmx:afterSettle does not scroll when the drawer is hidden", () => {
  const m = mark();
  const { drawer, handlers } = loadWithDrawer({ mark: m, hidden: true, overlay: false });
  handlers["htmx:afterSettle"][0]({ detail: { target: drawer } });
  assert.strictEqual(m._s, 0, "hidden drawer is not scrolled");
});

test("htmx:afterSettle focuses the close button only in the modal overlay", () => {
  // Desktop side-panel: no focus steal.
  const closeDesktop = focusable();
  const deskCtx = loadWithDrawer({ mark: mark(), closeBtn: closeDesktop, overlay: false });
  deskCtx.handlers["htmx:afterSettle"][0]({ detail: { target: deskCtx.drawer } });
  assert.ok(!closeDesktop.focused(), "desktop: close button not focused");
  // Narrow overlay (modal): focus lands on the close button.
  const closeOverlay = focusable();
  const ovCtx = loadWithDrawer({ mark: mark(), closeBtn: closeOverlay, overlay: true });
  ovCtx.handlers["htmx:afterSettle"][0]({ detail: { target: ovCtx.drawer } });
  assert.ok(closeOverlay.focused(), "overlay: close button focused");
});
