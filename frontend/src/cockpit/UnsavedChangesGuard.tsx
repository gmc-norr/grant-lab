import { useContext, useEffect } from "react";
import { UNSAFE_DataRouterContext, useBlocker } from "react-router-dom";

import { useEditorDirty } from "./editorStore";

/** Guards against silently discarding unsaved per-section editor buffers.
 *
 *  Two surfaces:
 *  1. `beforeunload` — reload / tab-close / external navigation while any buffer is
 *     dirty prompts the browser's native "leave site?" dialog.
 *  2. `useBlocker` (RouterLeaveGuard) — an in-app router navigation (back button,
 *     link, programmatic navigate) while dirty is intercepted; we confirm() before
 *     proceeding, and reset the blocker if the user cancels.
 *
 *  The internal artifact-tab switch (Draft↔Spec↔…) is NOT a router navigation, so it
 *  is guarded separately at its setTab source in Cockpit (confirmDiscardIfDirty).
 *
 *  useBlocker REQUIRES a data router (createBrowserRouter). The production app uses
 *  one; some tests mount Cockpit under a plain MemoryRouter. So the blocker half is
 *  rendered ONLY when a data-router context is present — the beforeunload half always
 *  runs, and the tab-switch confirm covers the in-app cockpit surface regardless. */
export function UnsavedChangesGuard() {
  const dirty = useEditorDirty();
  const isDirty = dirty.length > 0;
  const hasDataRouter = useContext(UNSAFE_DataRouterContext) != null;

  // (1) Native unload guard. Registered only while dirty so a clean cockpit never
  // triggers the browser's "leave site?" prompt.
  useEffect(() => {
    if (!isDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Legacy browsers require returnValue to be set to show the dialog.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [isDirty]);

  return hasDataRouter ? <RouterLeaveGuard isDirty={isDirty} /> : null;
}

// (2) Router navigation guard. Block only same-document navigations away while dirty;
// confirm to proceed, otherwise stay put. Isolated so useBlocker is only ever called
// under a data router.
function RouterLeaveGuard({ isDirty }: { isDirty: boolean }) {
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    const proceed = window.confirm(
      "You have unsaved edits. Leave this workspace and discard them?",
    );
    if (proceed) blocker.proceed();
    else blocker.reset();
  }, [blocker]);
  return null;
}
