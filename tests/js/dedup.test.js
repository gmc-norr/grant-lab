"use strict";
const test = require("node:test");
const assert = require("node:assert");
const { extractChatTurnId, stripPresentChatTurns } = require(
  "../../src/call_manager/web/static/dedup.js"
);

test("extractChatTurnId finds a chat-turn id (double quotes)", () => {
  assert.strictEqual(
    extractChatTurnId('<div class="chat-turn" id="chat-turn-abc-123"><p>hi</p></div>'),
    "chat-turn-abc-123"
  );
});

test("extractChatTurnId finds a chat-turn id (single quotes)", () => {
  assert.strictEqual(
    extractChatTurnId("<div class='chat-turn' id='chat-turn-xyz'></div>"),
    "chat-turn-xyz"
  );
});

test("extractChatTurnId returns null for a non-chat id (sse-mount)", () => {
  assert.strictEqual(
    extractChatTurnId('<div id="sse-mount-9"></div>'),
    null
  );
});

test("extractChatTurnId returns null for the done payload", () => {
  assert.strictEqual(extractChatTurnId("failed"), null);
  assert.strictEqual(extractChatTurnId("completed"), null);
});

test("stripPresentChatTurns drops a present chat-turn, keeps the connector", () => {
  const frag =
    '<div class="chat-turn" id="chat-turn-A"><p>echo</p><div class="ref-chip">x</div></div>' +
    '<div id="sse-mount-J"><div sse-swap="message"></div></div>';
  const present = new Set(["chat-turn-A"]);
  const out = stripPresentChatTurns(frag, (id) => present.has(id));
  assert.ok(!out.includes("chat-turn-A"), "present row removed");
  assert.ok(out.includes("sse-mount-J"), "connector preserved");
  assert.ok(out.includes("ref-chip") === false, "nested children of the dropped row gone");
});

test("stripPresentChatTurns keeps an absent chat-turn", () => {
  const frag = '<div class="chat-turn" id="chat-turn-B"><p>new</p></div>';
  const out = stripPresentChatTurns(frag, () => false);
  assert.strictEqual(out, frag);
});

test("stripPresentChatTurns keeps one, drops the other in a two-row fragment", () => {
  const frag =
    '<div class="chat-turn" id="chat-turn-A"><p>old</p></div>' +
    '<div class="chat-turn" id="chat-turn-B"><p>new</p></div>';
  const present = new Set(["chat-turn-A"]);
  const out = stripPresentChatTurns(frag, (id) => present.has(id));
  assert.ok(!out.includes("chat-turn-A"));
  assert.ok(out.includes("chat-turn-B"));
});

test("extractChatTurnId matches the real attribute order (data-role before id)", () => {
  assert.strictEqual(
    extractChatTurnId('<div class="chat-turn" data-role="user" id="chat-turn-Z"><p>x</p></div>'),
    "chat-turn-Z"
  );
});

test("extractChatTurnId does not false-match a data-*-id attribute", () => {
  // The #chat-history host has data-workspace-id="<uuid>"; even a contrived
  // value must not be mistaken for a chat-turn id (no standalone id= here).
  assert.strictEqual(
    extractChatTurnId('<div id="chat-history" data-workspace-id="chat-turn-FAKE" class="chat-history"></div>'),
    null
  );
});
