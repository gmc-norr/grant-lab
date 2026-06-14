// Small progressive-enhancement helpers, extracted from inline handlers so the
// app can ship a strict Content-Security-Policy (script-src 'self', no
// 'unsafe-inline'). Inline on* attributes and <script> blocks are blocked by
// that CSP; this file re-wires the same behavior via addEventListener.
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  ready(function () {
    // Controls that submit their enclosing form on change (filter bars):
    // <select data-autosubmit> and <input type="checkbox" data-autosubmit>.
    document.querySelectorAll("[data-autosubmit]").forEach(function (el) {
      el.addEventListener("change", function () {
        if (el.form) {
          el.form.submit();
        }
      });
    });

    // Forms that confirm before submitting (destructive actions).
    // Markup: <form data-confirm="Are you sure?"> ... </form>
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });

    // Profile-new "kind" toggle: show the field group matching the selected
    // profile kind. Guarded so it is a no-op on pages without the select.
    var kindSelect = document.getElementById("kind-select");
    if (kindSelect) {
      var toggleKind = function () {
        var k = kindSelect.value;
        var unit = document.getElementById("unit-fields");
        var individual = document.getElementById("individual-fields");
        if (unit) {
          unit.hidden = k !== "organizational_unit";
        }
        if (individual) {
          individual.hidden = k !== "individual";
        }
      };
      kindSelect.addEventListener("change", toggleKind);
      toggleKind();
    }
  });
})();

// Section-edit lock (best-effort; server 409s remain authoritative). Editing is
// disabled while a turn is in flight, and re-enabled only when BOTH (a) no SSE
// connector remains AND (b) no composer request is still awaiting its response.
// Tracking composer requests is what keeps a REJECTED submit (409/429/503/400 —
// which creates no connector) from leaving editing disabled forever, and stops
// an old connector's deferred cleanup from re-enabling while a new submit is
// mid-flight (the connector for it has not mounted yet).
var pendingComposerRequests = 0;

function reenable() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = false;
  });
}
function disableSectionEdits() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = true;
  });
}
function clearComposerErrorNotice() {
  // Remove any inline rejection banner left beside #chat-history by a prior
  // rejected submit (cap/lock/validation 4xx). The htmx:responseError handler
  // only swaps an OLD error for a NEWER error; a later SUCCESS has nothing to
  // swap, so without this the stale banner lingers — it sits beside
  // #chat-history, which the append-swap never touches. Scoped to the same
  // ":scope > .htmx-error-notice" placement responseError inserts at.
  var host = document.getElementById("chat-history");
  var parent = host && host.parentNode;
  if (!parent) return;
  var stale = parent.querySelectorAll(":scope > .htmx-error-notice");
  for (var i = 0; i < stale.length; i++) {
    var n = stale[i];
    if (n.remove) {
      n.remove();
    } else if (n.parentNode && n.parentNode.removeChild) {
      n.parentNode.removeChild(n);
    }
  }
}
function maybeReenable() {
  // Deferred to a macrotask so DOM mutations (connector add on swap, remove on
  // cleanup) have settled before we read them.
  setTimeout(function () {
    if (pendingComposerRequests === 0 && !document.querySelector('[id^="sse-mount-"]')) {
      reenable();
    }
  }, 0);
}

document.body.addEventListener("htmx:beforeRequest", function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === "composer") {
    pendingComposerRequests++;
    disableSectionEdits();
  }
});
document.body.addEventListener("htmx:afterRequest", function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === "composer") {
    pendingComposerRequests = Math.max(0, pendingComposerRequests - 1);
    // A successful turn submit clears any stale rejection banner from a prior
    // rejected attempt (the cap/lock/validation error that responseError can
    // only replace, never retire on its own).
    if (evt.detail.successful) {
      clearComposerErrorNotice();
    }
    // On success the connector is in the DOM by now (afterSwap precedes
    // afterRequest), so maybeReenable keeps editing disabled; on a rejected
    // submit there is no connector, so this is what re-enables it.
    maybeReenable();
  }
});

// A2: when an SSE connector ('sse-mount-*') is cleaned up (its job ended and the
// 'done' delete-child removed it), (1) refetch the artifact pane (idempotent —
// harmless after an unchanged/failed turn) and (2) attempt the combined re-enable.
document.body.addEventListener("htmx:beforeCleanupElement", function (evt) {
  var id = evt.detail.elt && evt.detail.elt.id;
  if (!id || id.indexOf("sse-mount-") !== 0) return;
  var host = document.getElementById("chat-history");
  var wsId = host && host.getAttribute("data-workspace-id");
  if (wsId && window.htmx) {
    window.htmx.ajax("GET", "/workspaces/" + wsId + "/artifact", {
      target: "#artifact-rendered",
      swap: "innerHTML",
    });
  }
  maybeReenable();
});

// A2 dedup — the SSE stream is workspace-global, so two live connectors can
// carry the same row. Suppress duplicates by stable id across BOTH swap paths
// (the SSE extension's own swap does not fire htmx:beforeSwap, so streamed rows
// need their own hook).
document.body.addEventListener("htmx:sseBeforeMessage", function (evt) {
  if (!window.cmDedup) return;
  var data = evt.detail && evt.detail.data;
  var id = window.cmDedup.extractChatTurnId(data);
  if (id && document.getElementById(id)) {
    evt.preventDefault(); // already in the DOM — skip this streamed row
  }
});
document.body.addEventListener("htmx:beforeSwap", function (evt) {
  var target = evt.detail && evt.detail.target;
  if (!target || target.id !== "chat-history") return;
  if (!window.cmDedup) return;
  var resp = evt.detail.serverResponse;
  if (typeof resp !== "string" || resp.indexOf("chat-turn-") === -1) return;
  evt.detail.serverResponse = window.cmDedup.stripPresentChatTurns(resp, function (id) {
    return !!document.getElementById(id);
  });
});

// A2 reconnect-on-render: a connector present on page load means a turn is live,
// so disable editing until it cleans up (symmetric with the re-enable above).
function disableSectionEditsIfConnectorPresent() {
  if (document.querySelector('[id^="sse-mount-"]')) {
    disableSectionEdits();
  }
}
if (document.readyState !== "loading") {
  disableSectionEditsIfConnectorPresent();
} else {
  document.addEventListener("DOMContentLoaded", disableSectionEditsIfConnectorPresent);
}

// Surface htmx error responses (4xx/5xx) as an inline notice near the
// triggering element. FastAPI errors are JSON {"detail": "..."}; HTML error
// fragments are inserted as text, not markup, to avoid injection.
document.body.addEventListener("htmx:responseError", function (evt) {
  var xhr = evt.detail.xhr;
  var msg = "request failed (" + xhr.status + ")";
  try {
    var body = JSON.parse(xhr.responseText);
    if (body && typeof body.detail === "string") msg = body.detail;
  } catch (e) { /* non-JSON body: keep generic message */ }
  var target = evt.detail.target || evt.detail.elt;
  var notice = document.createElement("p");
  notice.className = "htmx-error-notice";
  notice.setAttribute("role", "alert");
  notice.textContent = msg;
  // replace a previous notice for the same target rather than stacking
  var prev = target.parentNode.querySelector(":scope > .htmx-error-notice");
  if (prev) prev.remove();
  target.parentNode.insertBefore(notice, target);
});
