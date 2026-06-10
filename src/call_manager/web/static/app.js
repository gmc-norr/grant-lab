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
    // Selects that submit their enclosing form on change (filter bars).
    // Markup: <select data-autosubmit> ... </select>
    document.querySelectorAll("select[data-autosubmit]").forEach(function (el) {
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
