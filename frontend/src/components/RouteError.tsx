import { useEffect } from "react";
import { isRouteErrorResponse, useRouteError } from "react-router-dom";

// THE LAST-RESORT SCREEN, and it is deliberately PLAIN HTML with inline styles.
// Every other surface in the app may use Mantine; this one may not. It renders
// when a route has already failed, and the most common cause — a lazy chunk that
// 404s after a deploy — is exactly the kind of failure that can take the theme
// or a provider with it. The ErrorBoundary in App.tsx made the same choice for
// the same reason. Nothing here imports anything that can be the thing that
// broke.
//
// Review 2026-10-26 M-fe-2: there was no errorElement anywhere, so a route error
// replaced the whole app with react-router's developer screen —
// `mainNav=0 text="Unexpected Application Error! Failed to fetch dynamically
// imported module … 💿 Hey developer 👋 …"`. App.tsx's ErrorBoundary wraps
// RouterProvider and can never catch those: react-router's own
// RenderErrorBoundary sits inside it and takes them first.

/** The sessionStorage key guarding the automatic reload. Exported so the test
 *  asserts on the SAME key the component writes, not on a copy of the string. */
export const CHUNK_RELOAD_FLAG = "grant-lab:chunk-reload";

// A chunk-load failure, across the browsers and bundlers that word it
// differently. Chromium/Vite say "Failed to fetch dynamically imported module",
// Firefox "error loading dynamically imported module", Safari "Importing a
// module script failed", and webpack-era builds "Loading chunk N failed".
const CHUNK_ERROR =
  /failed to fetch dynamically imported module|error loading dynamically imported module|importing a module script failed|loading chunk \d+ failed|chunkloaderror/i;

export function isChunkLoadError(error: unknown): boolean {
  if (error instanceof Error) {
    return CHUNK_ERROR.test(error.name) || CHUNK_ERROR.test(error.message);
  }
  return typeof error === "string" && CHUNK_ERROR.test(error);
}

function detailOf(error: unknown): string | null {
  if (isRouteErrorResponse(error)) return `${error.status} ${error.statusText}`;
  if (error instanceof Error) return error.message;
  return null;
}

export function RouteError() {
  const error = useRouteError();
  const chunk = isChunkLoadError(error);

  // ONE automatic reload, and the flag is what makes it one. A deploy replaces
  // every hashed asset, so a tab held open since the previous release asks for a
  // file that is gone; reloading picks up the new index.html and the member
  // never sees this screen. Reloading UNCONDITIONALLY, though, is an infinite
  // loop the moment the chunk is permanently missing — a bad deploy, a proxy
  // that answers 404 for /assets — so the reload happens only when no flag is
  // already set, and setting it is part of the same read.
  //
  // The flag is never cleared on a successful boot, and that is on purpose: the
  // app itself boots FINE in this failure: only the lazy chunk 404s. Clearing it
  // on boot would hand the loop back its fuel.
  useEffect(() => {
    if (!chunk) return;
    let alreadyReloaded: boolean;
    try {
      alreadyReloaded = window.sessionStorage.getItem(CHUNK_RELOAD_FLAG) === "1";
      if (!alreadyReloaded) window.sessionStorage.setItem(CHUNK_RELOAD_FLAG, "1");
    } catch {
      // sessionStorage is not merely empty in some privacy modes — the ACCESS
      // THROWS (Safari's "The operation is insecure", a browser set to block
      // site data). With no readable flag there is no way to know whether this
      // is the first attempt or the fiftieth, so the conservative answer is to
      // reload zero times and let the member press the button. A reload that
      // cannot remember it has reloaded is a reload that never stops.
      return;
    }
    if (!alreadyReloaded) window.location.reload();
  }, [chunk]);

  const detail = detailOf(error);
  return (
    <main
      style={{
        maxWidth: "36rem",
        margin: "0 auto",
        padding: "3rem 1rem",
        fontFamily: "system-ui, sans-serif",
      }}
    >
      {/* The landmark and the live region are SEPARATE elements: role="alert" on
          the <main> itself would override its implicit landmark role, and the
          e2e walk counts landmarks as its proof that a page rendered at all. */}
      <div role="alert">
        <h1 style={{ fontSize: "1.25rem", margin: "0 0 0.5rem" }}>Couldn&apos;t load this page</h1>
        <p style={{ margin: "0 0 1rem", lineHeight: 1.5 }}>
          {chunk
            ? "Grant Lab was updated while this tab was open, so part of it is no longer available. Reloading picks up the new version."
            : "Something went wrong rendering this page. Reloading often clears it; if it keeps happening, tell the Grant Lab maintainers what you were doing."}
        </p>
        {detail !== null && (
          <p
            style={{
              margin: "0 0 1rem",
              fontSize: "0.8125rem",
              opacity: 0.7,
              wordBreak: "break-word",
            }}
          >
            {detail}
          </p>
        )}
      </div>
      <p style={{ display: "flex", gap: "0.75rem", alignItems: "center", margin: 0 }}>
        <button type="button" onClick={() => window.location.reload()}>
          Reload
        </button>
        {/* A PLAIN ANCHOR, not a react-router Link: the router is the thing that
            just failed, and a client-side navigation would re-enter the same
            broken tree. A full document load is the way out. The review's
            complaint about react-router's screen was "mainNav=0" — no way to
            get anywhere from it at all. */}
        <a href="/">Back to calls</a>
      </p>
    </main>
  );
}
