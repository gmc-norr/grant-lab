import type { Editor } from "@tiptap/core";
import { useEffect } from "react";

import { subscribeSectionJump, takePendingSectionJump } from "../cockpit/sectionJump";

// The section index the backend hands the sidebar is defined by
// proposal_assist/editing/sectionizer.py: **section 0 is always the preamble**
// (the text before the first heading, possibly empty), and section N >= 1 starts
// at the Nth `##` line — H2 exactly, never `###`, and never inside a ``` fence.
//
// So in the Tiptap document, section N (N >= 1) is the Nth `heading` node with
// level 2, counted in document order. Section 0 has no heading node of its own,
// so it resolves to the top of the document.

/** The DOM element to scroll to for *index*, or null if the document does not
 *  hold that section yet (a Yjs doc syncs after mount — it is empty at first). */
export function sectionHeadingDom(editor: Editor, index: number): HTMLElement | null {
  if (index <= 0) {
    // The preamble has no heading of its own; its start IS the top of the doc.
    const top = editor.view.dom;
    return top instanceof HTMLElement ? top : null;
  }
  let seen = 0;
  let found: number | null = null;
  editor.state.doc.descendants((node, pos) => {
    if (found !== null) return false;
    if (node.type.name === "heading" && node.attrs.level === 2) {
      seen += 1;
      if (seen === index) found = pos;
    }
    return true;
  });
  if (found === null) return null;
  const dom = editor.view.nodeDOM(found);
  return dom instanceof HTMLElement ? dom : null;
}

/** Scroll the artifact to whichever section the Outline asks for.
 *
 *  Retries on every document update rather than giving up: a click that also
 *  switches artifact tab mounts this editor against an EMPTY Yjs doc, and the
 *  real content only arrives when the provider syncs a moment later. Each
 *  request is consumed exactly once, so a synced document does not re-scroll on
 *  every keystroke and fight the user. */
export function useSectionJump(editor: Editor | null): void {
  useEffect(() => {
    if (!editor) return;
    // A request made before this editor existed (tab switch, deep link).
    let want: number | null = takePendingSectionJump();

    const attempt = () => {
      if (want === null) return;
      const dom = sectionHeadingDom(editor, want);
      if (!dom) return; // not synced yet — the next update tries again
      want = null; // one scroll per request
      dom.scrollIntoView({ block: "start", behavior: "smooth" });
    };

    attempt();
    const off = subscribeSectionJump((index) => {
      want = index;
      attempt();
    });
    editor.on("update", attempt);
    return () => {
      off();
      editor.off("update", attempt);
    };
  }, [editor]);
}
