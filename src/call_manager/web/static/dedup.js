// Pure, DOM-free de-duplication helpers for the A2 live-turn monitor.
// Loaded in the browser (sets window.cmDedup) AND importable by node --test
// (CommonJS module.exports). No window/document access here — the browser
// glue in app.js injects the `hasId` predicate.
(function () {
  "use strict";

  // Pull the chat-turn id out of a rendered row fragment. Returns null for a
  // fragment with no chat-turn id (e.g. the connector div, or the 'done'
  // payload string), so callers can pass anything through safely.
  function extractChatTurnId(html) {
    if (typeof html !== "string") return null;
    var m = /(?:^|\s)id=["']?(chat-turn-[^"'\s>]+)/.exec(html);
    return m ? m[1] : null;
  }

  // Walk top-level <div>…</div> blocks (depth-counted so nested divs like
  // .ref-chip don't confuse the boundary) and drop any whose chat-turn id
  // satisfies hasId(id). Non-div text and non-chat divs (the sse-mount
  // connector) pass through unchanged.
  function stripPresentChatTurns(html, hasId) {
    if (typeof html !== "string") return html;
    var out = "";
    var i = 0;
    var n = html.length;
    while (i < n) {
      var open = html.indexOf("<div", i);
      if (open === -1) {
        out += html.slice(i);
        break;
      }
      out += html.slice(i, open); // text before this div block
      // Depth-count to the matching </div>.
      var depth = 1;
      var j = open + 4;
      while (j < n && depth > 0) {
        var nextOpen = html.indexOf("<div", j);
        var nextClose = html.indexOf("</div>", j);
        if (nextClose === -1) {
          j = n;
          break;
        }
        if (nextOpen !== -1 && nextOpen < nextClose) {
          depth++;
          j = nextOpen + 4;
        } else {
          depth--;
          j = nextClose + 6;
        }
      }
      var blockEnd = depth === 0 ? j : n;
      var block = html.slice(open, blockEnd);
      var openTag = block.slice(0, block.indexOf(">") + 1);
      var id = extractChatTurnId(openTag);
      if (!(id && hasId(id))) {
        out += block; // keep it
      }
      i = blockEnd;
    }
    return out;
  }

  var api = { extractChatTurnId: extractChatTurnId, stripPresentChatTurns: stripPresentChatTurns };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.cmDedup = api;
  }
})();
