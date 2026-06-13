"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "../../src/call_manager/web/static/app.js");
const dedup = require("../../src/call_manager/web/static/dedup.js");

// Load app.js into a fresh vm context with DOM/window stubs.
//   opts.present  : Set of element ids for which getElementById returns truthy
//                   (drives the dedup hooks' "already in the DOM" check).
//   opts.connector: whether document.querySelector('[id^="sse-mount-"]') finds
//                   a live connector (drives the section-edit re-enable gate).
// Returns { handlers, context, buttons, setConnector }.
function loadApp(opts) {
  opts = opts || {};
  const presentSet = opts.present || new Set();
  const state = { connector: !!opts.connector };
  const buttons = [{ disabled: false }, { disabled: false }];
  const handlers = {};
  const body = {
    addEventListener: function (name, fn) {
      (handlers[name] = handlers[name] || []).push(fn);
    },
  };
  const documentStub = {
    body: body,
    readyState: "complete",
    addEventListener: function () {},
    getElementById: function (id) {
      return presentSet.has(id)
        ? { id: id, getAttribute: function () { return "ws-1"; } }
        : null;
    },
    querySelector: function (sel) {
      if (sel && sel.indexOf("sse-mount-") !== -1) {
        return state.connector ? { id: "sse-mount-x" } : null;
      }
      return null;
    },
    querySelectorAll: function (sel) {
      return sel === ".section-edit-btn" ? buttons : [];
    },
  };
  const context = {
    document: documentStub,
    window: { cmDedup: dedup, htmx: { ajax: function () {} } },
    setTimeout: function (fn) {
      fn(); // run deferred callbacks synchronously in tests
    },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), context);
  return {
    handlers: handlers,
    context: context,
    buttons: buttons,
    setConnector: function (v) { state.connector = !!v; },
  };
}

function fire(handlers, name, detail) {
  let prevented = false;
  const evt = { detail: detail, preventDefault: function () { prevented = true; } };
  (handlers[name] || []).forEach(function (fn) { fn(evt); });
  return { evt: evt, prevented: prevented };
}

// --- dedup hooks ---

test("sseBeforeMessage prevents default for an already-present row", function () {
  const { handlers } = loadApp({ present: new Set(["chat-turn-A"]) });
  const res = fire(handlers, "htmx:sseBeforeMessage", {
    data: '<div class="chat-turn" id="chat-turn-A"><p>x</p></div>',
  });
  assert.strictEqual(res.prevented, true);
});

test("sseBeforeMessage allows a new row", function () {
  const { handlers } = loadApp({ present: new Set() });
  const res = fire(handlers, "htmx:sseBeforeMessage", {
    data: '<div class="chat-turn" id="chat-turn-B"><p>x</p></div>',
  });
  assert.strictEqual(res.prevented, false);
});

test("beforeSwap strips a present row from the POST response, keeps the connector", function () {
  const { handlers } = loadApp({ present: new Set(["chat-turn-A"]) });
  const detail = {
    target: { id: "chat-history" },
    serverResponse:
      '<div class="chat-turn" id="chat-turn-A"><p>x</p></div><div id="sse-mount-J"></div>',
  };
  fire(handlers, "htmx:beforeSwap", detail);
  assert.ok(!detail.serverResponse.includes("chat-turn-A"));
  assert.ok(detail.serverResponse.includes("sse-mount-J"));
});

test("beforeSwap ignores swaps not targeting #chat-history", function () {
  const { handlers } = loadApp({ present: new Set(["chat-turn-A"]) });
  const detail = {
    target: { id: "artifact-rendered" },
    serverResponse: '<div class="chat-turn" id="chat-turn-A"></div>',
  };
  fire(handlers, "htmx:beforeSwap", detail);
  assert.ok(detail.serverResponse.includes("chat-turn-A"));
});

// --- section-edit re-enable state machine (the Codex High fix) ---

test("rejected submit (no connector) re-enables section editing", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  assert.strictEqual(app.context.pendingComposerRequests, 1);
  assert.ok(app.buttons.every(function (b) { return b.disabled === true; }), "disabled during request");
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" } });
  assert.strictEqual(app.context.pendingComposerRequests, 0);
  assert.ok(app.buttons.every(function (b) { return b.disabled === false; }), "re-enabled after rejected submit");
});

test("success path: editing stays disabled until the connector cleans up", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  app.setConnector(true); // connector mounted during the successful swap
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" } });
  assert.ok(app.buttons.every(function (b) { return b.disabled === true; }), "stays disabled while a connector is live");
  app.setConnector(false); // 'done' delete-child removed it
  fire(app.handlers, "htmx:beforeCleanupElement", { elt: { id: "sse-mount-J" } });
  assert.ok(app.buttons.every(function (b) { return b.disabled === false; }), "re-enabled after the connector cleans up");
});

test("the edit lock ignores non-composer requests", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "something-else" } });
  assert.strictEqual(app.context.pendingComposerRequests, 0);
});
