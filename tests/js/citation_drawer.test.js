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
