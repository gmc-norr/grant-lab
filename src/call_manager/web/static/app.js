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

  // ---- Reference drop zone (batch PDF upload) -------------------------

  function filterPdfs(files, caps) {
    var accepted = [], rejected = [];
    var total = 0;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      var isPdf = f.type === "application/pdf" || /\.pdf$/i.test(f.name || "");
      if (isPdf) { accepted.push(f); total += f.size || 0; } else { rejected.push(f); }
    }
    if (accepted.length > caps.maxFiles)
      return { accepted: [], rejected: rejected, error: "Too many files (max " + caps.maxFiles + " per drop)." };
    if (total > caps.maxBytes)
      return { accepted: [], rejected: rejected, error: "Batch too large (max " + caps.maxBytes + " bytes)." };
    return { accepted: accepted, rejected: rejected, error: null };
  }

  // Drives files through queued->processing->done/failed with <=concurrency in
  // flight. uploadOne(file,row)->Promise<statusCode>; 429 requeues the SAME row
  // after backoff. Returns a Promise resolving when the queue drains.
  function runQueue(files, opts) {
    var concurrency = opts.concurrency || 3;
    var backoffMs = opts.backoffMs || 2000;
    // Allow caller to inject a delay function for testing (opts._delay); fall
    // back to a real setTimeout-based delay, or a zero-tick Promise if
    // setTimeout is unavailable (e.g. vm unit-test sandboxes).
    var delay = opts._delay || (typeof setTimeout === "function"
      ? function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
      : function () { return Promise.resolve(); });
    var items = [];
    for (var i = 0; i < files.length; i++) {
      items.push({ file: files[i], row: opts.makeRow(files[i]) });
    }
    var idx = 0, active = 0;
    return new Promise(function (resolve) {
      function settle() { if (active === 0 && idx >= items.length) resolve(); }
      function pump() {
        while (active < concurrency && idx < items.length) {
          var item = items[idx++];
          active++;
          item.row.set("processing");
          (function (it) {
            Promise.resolve(opts.uploadOne(it.file, it.row))
              .then(function (status) {
                if (status === 429) {
                  it.row.set("queued");
                  delay(backoffMs).then(function () { items.push(it); active--; pump(); settle(); });
                  return;
                }
                active--; pump(); settle();
              })
              .catch(function () { it.row.set("failed"); active--; pump(); settle(); });
          })(item);
        }
        settle();
      }
      if (!items.length) resolve(); else pump();
    });
  }

  if (typeof window !== "undefined")
    window.__refsDropzone = { filterPdfs: filterPdfs, runQueue: runQueue };

  ready(function () {
    var zone = document.getElementById && document.getElementById("reference-dropzone");
    if (!zone) return;
    var input = document.getElementById("reference-dropzone-input");
    var list = document.getElementById("reference-list");
    var postUrl = zone.getAttribute("data-post-url");
    var caps = {
      maxFiles: parseInt(zone.getAttribute("data-max-files"), 10) || 25,
      maxBytes: parseInt(zone.getAttribute("data-max-bytes"), 10) || 209715200,
    };

    function makeRow(file) {
      var row = document.createElement("div");
      row.className = "reference-row reference-pending";
      if (list) list.insertBefore(row, list.firstChild);
      return {
        el: row,
        set: function (state) {
          row.className = "reference-row reference-" + state;
          row.textContent = file.name + " — " + state;
          if (state === "failed") {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.textContent = "Retry";
            btn.addEventListener("click", function () {
              row.remove();
              runQueue([file], { concurrency: 1, uploadOne: uploadOne, makeRow: makeRow });
            });
            row.appendChild(btn);
          }
        },
      };
    }

    function uploadOne(file, row) {
      var fd = new FormData();
      fd.append("file", file);
      fd.append("auto_classify", "true");
      return fetch(postUrl, { method: "POST", body: fd }).then(function (resp) {
        if (resp.status === 429) return 429;
        return resp.text().then(function (html) {
          if (!resp.ok) { row.set("failed"); return resp.status; }
          row.el.outerHTML = html; // swap placeholder for the real partial
          return 200;
        });
      }).catch(function () { row.set("failed"); return 0; });
    }

    function handle(fileList) {
      var res = filterPdfs(Array.prototype.slice.call(fileList || []), caps);
      if (res.error) { window.alert(res.error); return; }
      if (res.rejected.length)
        window.alert("Skipped non-PDF files. Use the upload form below for data files (CSV, XLSX, …).");
      runQueue(res.accepted, { concurrency: 3, uploadOne: uploadOne, makeRow: makeRow });
    }

    zone.addEventListener("click", function () { if (input) input.click(); });
    if (input) input.addEventListener("change", function () { handle(input.files); });
    zone.addEventListener("dragover", function (e) { e.preventDefault(); zone.classList.add("dragover"); });
    zone.addEventListener("dragleave", function () { zone.classList.remove("dragover"); });
    zone.addEventListener("drop", function (e) {
      e.preventDefault();
      zone.classList.remove("dragover");
      handle(e.dataTransfer && e.dataTransfer.files);
    });
  });
})();

// Section-edit + send lock (best-effort; server 409s remain authoritative).
// Editing AND the composer's Send button are disabled while a turn is in
// flight, and re-enabled only when BOTH (a) no SSE connector remains AND (b) no
// composer request is still awaiting its response. Tracking composer requests
// is what keeps a REJECTED submit (409/429/503/400 — which creates no
// connector) from leaving the controls disabled forever, and stops an old
// connector's deferred cleanup from re-enabling while a new submit is mid-flight
// (the connector for it has not mounted yet).
var pendingComposerRequests = 0;
// Safety valve: document.body / document.querySelector may be absent in
// unit-test vm stubs that only need the IIFE-exported helpers
// (e.g. reference_dropzone.test.js).
var docBody = document.body || { addEventListener: function () {} };
function docQS(sel) {
  return document.querySelector ? document.querySelector(sel) : null;
}

function setSendBusy(busy) {
  // Give the Send button a visible "working" state (color via .is-working +
  // a "Working…" label) and disable it while a turn from THIS tab is live, so
  // it is obvious the agent is running and a second identical submit can't be
  // fired by accident. Cross-tab stacking is unaffected (a fresh tab has no
  // local connector). The idle label is captured once so it restores exactly.
  var btn = docQS('#composer button[type="submit"]');
  if (!btn) return;
  btn.disabled = busy;
  if (busy) {
    if (btn.classList) btn.classList.add("is-working");
    if (btn._idleLabel === undefined) btn._idleLabel = btn.textContent;
    btn.textContent = "Working…";
  } else {
    if (btn.classList) btn.classList.remove("is-working");
    if (btn._idleLabel !== undefined) {
      btn.textContent = btn._idleLabel;
      btn._idleLabel = undefined;
    }
  }
}
function reenable() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = false;
  });
  setSendBusy(false);
}
function disableSectionEdits() {
  document.querySelectorAll(".section-edit-btn").forEach(function (b) {
    b.disabled = true;
  });
  setSendBusy(true);
}

// Auto-scroll the chat audit trail to the newest turn. The trail is a fixed-
// height scroll region; without this, an appended user turn / "Working…" card /
// streamed agent row lands below the fold and the user can't tell anything
// happened (and re-submits). We stick to the bottom only when the user is
// already near it, so scrolling up to read history is never hijacked.
function isNearChatBottom(el, threshold) {
  if (threshold == null) threshold = 120;
  return el.scrollHeight - el.scrollTop - el.clientHeight <= threshold;
}
function scrollChatToBottom(force) {
  var el = document.getElementById("chat-history");
  if (!el) return;
  if (force || isNearChatBottom(el)) {
    el.scrollTop = el.scrollHeight;
  }
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
    if (pendingComposerRequests === 0 && !docQS('[id^="sse-mount-"]')) {
      reenable();
    }
  }, 0);
}

docBody.addEventListener("htmx:beforeRequest", function (evt) {
  if (!(evt.detail.elt && evt.detail.elt.id === "composer")) return;
  // Guard against a duplicate/stacked submit from THIS tab while a turn is
  // already in flight (a connector is mounted) or a prior submit has not
  // returned yet — the exact "nothing happened, click/Enter again" double-send
  // that silently burns a whole agent run. The disabled Send button stops most
  // clicks; this also catches Enter-key submits. Cross-tab stacking is
  // unaffected (a fresh tab has no local connector).
  if (pendingComposerRequests > 0 || docQS('[id^="sse-mount-"]')) {
    evt.preventDefault();
    return;
  }
  pendingComposerRequests++;
  disableSectionEdits();
});
docBody.addEventListener("htmx:afterRequest", function (evt) {
  if (evt.detail.elt && evt.detail.elt.id === "composer") {
    pendingComposerRequests = Math.max(0, pendingComposerRequests - 1);
    // A successful turn submit clears any stale rejection banner from a prior
    // rejected attempt (the cap/lock/validation error that responseError can
    // only replace, never retire on its own) and jumps the trail to the newest
    // turn (you just sent — always follow it, even if you'd scrolled up).
    if (evt.detail.successful) {
      clearComposerErrorNotice();
      scrollChatToBottom(true);
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
docBody.addEventListener("htmx:beforeCleanupElement", function (evt) {
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
docBody.addEventListener("htmx:sseBeforeMessage", function (evt) {
  if (!window.cmDedup) return;
  var data = evt.detail && evt.detail.data;
  var id = window.cmDedup.extractChatTurnId(data);
  if (id && document.getElementById(id)) {
    evt.preventDefault(); // already in the DOM — skip this streamed row
  }
});
docBody.addEventListener("htmx:beforeSwap", function (evt) {
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
  if (docQS('[id^="sse-mount-"]')) {
    disableSectionEdits();
  }
}
if (document.readyState !== "loading") {
  disableSectionEditsIfConnectorPresent();
} else {
  document.addEventListener("DOMContentLoaded", disableSectionEditsIfConnectorPresent);
}

// Chat auto-scroll (chat-client behaviour): keep the trail pinned to the newest
// turn as content is appended — your sent turn, streamed agent rows, the
// "Working…" connector — but only when you are already near the bottom, so
// scrolling up to read earlier turns is never hijacked. A MutationObserver
// catches every append path regardless of which htmx event (or the SSE
// extension's own swap) produced it.
function startChatAutoScroll() {
  var el = document.getElementById("chat-history");
  if (!el || typeof MutationObserver === "undefined") return;
  new MutationObserver(function () {
    scrollChatToBottom(false);
  }).observe(el, { childList: true, subtree: true });
  scrollChatToBottom(true); // land on the latest turn on initial load
}
if (document.readyState !== "loading") {
  startChatAutoScroll();
} else {
  document.addEventListener("DOMContentLoaded", startChatAutoScroll);
}

// Surface htmx error responses (4xx/5xx) as an inline notice near the
// triggering element. FastAPI errors are JSON {"detail": "..."}; HTML error
// fragments are inserted as text, not markup, to avoid injection.
docBody.addEventListener("htmx:responseError", function (evt) {
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

// --- Citation source viewer: open the reference drawer from a cite link ---
var READER_HREF_RE = /^\/workspaces\/[^/]+\/citations\/ev-[0-9a-f]{6,}\/reader$/;

function citeReaderUrl(el) {
  // Only links WE injected carry class="cite" (agent HTML can't — no class in
  // the bleach allowlist) AND match the exact reader-route shape.
  if (!el || !el.classList || !el.classList.contains("cite")) return null;
  var href = el.getAttribute ? el.getAttribute("href") : null;
  return href && READER_HREF_RE.test(href) ? href : null;
}

function readerIsOverlay() {
  return !!(window.matchMedia && window.matchMedia("(max-width: 1023.98px)").matches);
}

function applyReaderModal(open) {
  // Modal semantics (scroll lock + aria-modal) are NARROW-ONLY.
  var drawer = document.getElementById("reference-drawer");
  var overlay = open && readerIsOverlay();
  if (document.body && document.body.classList) {
    document.body.classList[overlay ? "add" : "remove"]("reader-scroll-lock");
  }
  if (drawer && drawer.setAttribute) {
    drawer.setAttribute("aria-modal", overlay ? "true" : "false");
  }
}

function openReaderDrawer(url, triggerEl) {
  var drawer = document.getElementById("reference-drawer");
  var backdrop = document.getElementById("reference-drawer-backdrop");
  if (!drawer) return;
  if (triggerEl) drawer._returnFocusTo = triggerEl;
  if (window.htmx) {
    window.htmx.ajax("GET", url, { target: "#reference-drawer", swap: "innerHTML" });
  }
  drawer.hidden = false;
  if (backdrop) backdrop.hidden = !readerIsOverlay();
  applyReaderModal(true);
  // The scroll-to-evidence + focus run on htmx:afterSettle (see below): the GET
  // swaps the fragment in asynchronously, so #ev-target isn't in the DOM yet.
}

// Once the reader fragment has settled into #reference-drawer, jump to the
// highlighted evidence. Covers first-open AND prev/next nav (both swap here).
function focusReaderEvidence() {
  var drawer = document.getElementById("reference-drawer");
  if (!drawer || drawer.hidden) return;
  var mark = document.getElementById("ev-target");
  if (mark && mark.scrollIntoView) mark.scrollIntoView({ block: "center" });
  // Modal overlay only: pull focus into the dialog now that its content exists.
  if (readerIsOverlay()) {
    var closeBtn = drawer.querySelector ? drawer.querySelector("[data-reader-close]") : null;
    if (closeBtn && closeBtn.focus) closeBtn.focus();
  }
}

function closeReaderDrawer() {
  var drawer = document.getElementById("reference-drawer");
  var backdrop = document.getElementById("reference-drawer-backdrop");
  if (!drawer) return;
  drawer.hidden = true;
  if (backdrop) backdrop.hidden = true;
  applyReaderModal(false);
  var back = drawer._returnFocusTo;
  if (back && back.focus) back.focus();
}

function readerFocusables() {
  var drawer = document.getElementById("reference-drawer");
  if (!drawer || !drawer.querySelectorAll) return [];
  // Close button, prev/next nav links, and any explicitly focusable node.
  // Disabled controls and tabindex="-1" (incl. the drawer container) excluded.
  var nodes = drawer.querySelectorAll(
    'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  return Array.prototype.slice.call(nodes);
}

function trapReaderFocus(evt) {
  // Tab trap is NARROW-ONLY (modal). The desktop side-panel is non-modal, so
  // focus is free to flow back to the proposal — same policy as scroll-lock.
  var drawer = document.getElementById("reference-drawer");
  if (!drawer || drawer.hidden || !readerIsOverlay()) return;
  var items = readerFocusables();
  if (!items.length) {
    // Transient (content not swapped in yet): keep focus on the dialog itself.
    if (evt.preventDefault) evt.preventDefault();
    if (drawer.focus) drawer.focus();
    return;
  }
  var first = items[0];
  var last = items[items.length - 1];
  var active = document.activeElement;
  var inside = drawer.contains ? drawer.contains(active) : true;
  if (evt.shiftKey) {
    if (!inside || active === first) {
      if (evt.preventDefault) evt.preventDefault();
      if (last.focus) last.focus();
    }
  } else if (!inside || active === last) {
    if (evt.preventDefault) evt.preventDefault();
    if (first.focus) first.focus();
  }
}

docBody.addEventListener("click", function (evt) {
  var t = evt.target;
  var citeEl = t && t.closest ? t.closest("a.cite") : null;
  var citeUrl = citeEl ? citeReaderUrl(citeEl) : null;
  if (citeUrl) {
    if (evt.preventDefault) evt.preventDefault();
    openReaderDrawer(citeUrl, citeEl);
    return;
  }
  // prev/next inside the drawer
  var navEl = t && t.closest ? t.closest("a.cite-nav") : null;
  if (navEl && navEl.getAttribute && READER_HREF_RE.test(navEl.getAttribute("href") || "")) {
    if (evt.preventDefault) evt.preventDefault();
    var drawer = document.getElementById("reference-drawer");
    openReaderDrawer(navEl.getAttribute("href"), drawer && drawer._returnFocusTo);
    return;
  }
  if (t && t.closest && t.closest("[data-reader-close], #reference-drawer-backdrop")) {
    if (evt.preventDefault) evt.preventDefault();
    closeReaderDrawer();
  }
});

docBody.addEventListener("keydown", function (evt) {
  var drawer = document.getElementById("reference-drawer");
  if (!drawer || drawer.hidden) return;
  if (evt.key === "Escape") {
    closeReaderDrawer();
  } else if (evt.key === "Tab") {
    trapReaderFocus(evt);
  }
});

docBody.addEventListener("htmx:afterSettle", function (evt) {
  var swapped = (evt.detail && evt.detail.target) || evt.target;
  if (!swapped || swapped.id !== "reference-drawer") return;
  focusReaderEvidence();
});
