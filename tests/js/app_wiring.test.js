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

  // Minimal DOM model for the inline error-notice region: #chat-history and
  // its parent. responseError inserts a <p class="htmx-error-notice"> as a
  // sibling before #chat-history; the success path clears it. `notices` is the
  // live list of those banners currently under the parent.
  const notices = [];
  const chatHistoryParent = {
    querySelector: function (sel) {
      if (sel === ":scope > .htmx-error-notice") {
        return notices.length ? notices[notices.length - 1] : null;
      }
      return null;
    },
    querySelectorAll: function (sel) {
      // Return a snapshot (like a static NodeList) so callers can remove while iterating.
      return sel === ":scope > .htmx-error-notice" ? notices.slice() : [];
    },
    insertBefore: function (node /* , ref */) {
      node.parentNode = this;
      if (node.className === "htmx-error-notice") notices.push(node);
    },
  };
  // Scroll geometry for the trail; tests can preset it via opts.scroll.
  const scroll = Object.assign(
    { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 },
    opts.scroll || {}
  );
  const chatHistoryEl = {
    id: "chat-history",
    parentNode: chatHistoryParent,
    getAttribute: function () { return "ws-1"; },
    get scrollTop() { return scroll.scrollTop; },
    set scrollTop(v) { scroll.scrollTop = v; },
    get scrollHeight() { return scroll.scrollHeight; },
    get clientHeight() { return scroll.clientHeight; },
  };

  // The composer's Send button, with a class recorder so tests can assert the
  // .is-working color state alongside disabled/label.
  const sendButton = {
    disabled: false,
    textContent: "Send",
    classList: (function () {
      const set = new Set();
      return {
        add: function (c) { set.add(c); },
        remove: function (c) { set.delete(c); },
        contains: function (c) { return set.has(c); },
      };
    })(),
  };

  const documentStub = {
    body: body,
    readyState: "complete",
    addEventListener: function () {},
    createElement: function () {
      return {
        className: "",
        textContent: "",
        parentNode: null,
        setAttribute: function () {},
        remove: function () {
          const i = notices.indexOf(this);
          if (i !== -1) notices.splice(i, 1);
          this.parentNode = null;
        },
      };
    },
    getElementById: function (id) {
      if (id === "chat-history") return chatHistoryEl;
      return presentSet.has(id)
        ? { id: id, getAttribute: function () { return "ws-1"; } }
        : null;
    },
    querySelector: function (sel) {
      if (sel === '#composer button[type="submit"]') return sendButton;
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
    chatHistory: chatHistoryEl,
    sendButton: sendButton,
    scroll: scroll,
    notices: function () { return notices.slice(); },
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

// --- stale rejection banner (the cap-error-survives-a-success bug) ---

function rejectComposer(app, status, detail) {
  // Simulate a rejected composer submit: htmx fires responseError with the
  // hx-target (#chat-history) as detail.target; app.js renders an inline notice.
  fire(app.handlers, "htmx:responseError", {
    xhr: { status: status, responseText: JSON.stringify({ detail: detail }) },
    target: app.chatHistory,
  });
}

test("a rejected composer submit renders one inline error banner", function () {
  const app = loadApp({});
  rejectComposer(app, 429, "cap exceeded");
  const notices = app.notices();
  assert.strictEqual(notices.length, 1);
  assert.strictEqual(notices[0].className, "htmx-error-notice");
  assert.strictEqual(notices[0].textContent, "cap exceeded");
});

test("a successful composer submit clears the stale rejection banner", function () {
  const app = loadApp({});
  rejectComposer(app, 429, "cap exceeded"); // first attempt: rejected
  assert.strictEqual(app.notices().length, 1, "banner shown after rejection");
  // Second attempt succeeds (cap raised). The stale banner must not linger.
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" }, successful: true });
  assert.strictEqual(app.notices().length, 0, "success clears the stale banner");
});

test("a still-rejected resubmit keeps exactly one (current) banner", function () {
  const app = loadApp({});
  rejectComposer(app, 429, "cap exceeded");
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" }, successful: false });
  rejectComposer(app, 503, "drafting busy"); // a different rejection
  const notices = app.notices();
  assert.strictEqual(notices.length, 1, "no stacking — replace, not accumulate");
  assert.strictEqual(notices[0].textContent, "drafting busy", "shows the latest error");
});

// --- chat auto-scroll (stick-to-bottom like a chat client) ---

test("isNearChatBottom: true at the bottom, false when scrolled up", function () {
  const app = loadApp({ scroll: { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 } });
  // distance from bottom = 1000 - 600 - 400 = 0 -> near
  assert.strictEqual(app.context.isNearChatBottom(app.chatHistory), true);
  app.scroll.scrollTop = 100; // distance = 1000 - 100 - 400 = 500 -> far
  assert.strictEqual(app.context.isNearChatBottom(app.chatHistory), false);
});

test("scrollChatToBottom(false) sticks only when already near the bottom", function () {
  const app = loadApp({ scroll: { scrollTop: 100, scrollHeight: 1000, clientHeight: 400 } });
  app.context.scrollChatToBottom(false); // scrolled up: must NOT yank down
  assert.strictEqual(app.scroll.scrollTop, 100, "left alone while reading history");
  app.scroll.scrollTop = 600; // now near the bottom
  app.context.scrollChatToBottom(false);
  assert.strictEqual(app.scroll.scrollTop, 1000, "follows new content when near bottom");
});

test("scrollChatToBottom(true) always jumps to the bottom (you just sent)", function () {
  const app = loadApp({ scroll: { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 } });
  app.context.scrollChatToBottom(true);
  assert.strictEqual(app.scroll.scrollTop, 1000);
});

test("a successful composer submit force-scrolls to the newest turn", function () {
  const app = loadApp({ scroll: { scrollTop: 0, scrollHeight: 1000, clientHeight: 400 } });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" }, successful: true });
  assert.strictEqual(app.scroll.scrollTop, 1000, "jumps to the latest after sending");
});

// --- Send button working state + double-submit guard ---

test("composer beforeRequest puts Send into a visible working state", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  assert.strictEqual(app.sendButton.disabled, true);
  assert.strictEqual(app.sendButton.textContent, "Working…");
  assert.ok(app.sendButton.classList.contains("is-working"), "gets the .is-working color class");
});

test("a rejected submit restores the Send button to idle", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" }, successful: false });
  assert.strictEqual(app.sendButton.disabled, false);
  assert.strictEqual(app.sendButton.textContent, "Send");
  assert.ok(!app.sendButton.classList.contains("is-working"));
});

test("Send stays working through a successful turn until the connector cleans up", function () {
  const app = loadApp({ connector: false });
  fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  app.setConnector(true); // connector mounted on the successful swap
  fire(app.handlers, "htmx:afterRequest", { elt: { id: "composer" }, successful: true });
  assert.strictEqual(app.sendButton.disabled, true, "stays working while the agent runs");
  assert.strictEqual(app.sendButton.textContent, "Working…");
  app.setConnector(false);
  fire(app.handlers, "htmx:beforeCleanupElement", { elt: { id: "sse-mount-J" } });
  assert.strictEqual(app.sendButton.disabled, false, "back to idle when the turn ends");
  assert.strictEqual(app.sendButton.textContent, "Send");
});

test("a second submit while a turn is live is blocked (no double-send)", function () {
  const app = loadApp({ connector: true }); // a turn is already in flight
  const res = fire(app.handlers, "htmx:beforeRequest", { elt: { id: "composer" } });
  assert.strictEqual(res.prevented, true, "the duplicate request is cancelled");
  assert.strictEqual(app.context.pendingComposerRequests, 0, "not counted as a new in-flight submit");
});
