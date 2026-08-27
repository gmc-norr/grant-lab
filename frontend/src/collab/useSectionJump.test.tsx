import { Editor } from "@tiptap/core";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { requestSectionJump, resetSectionJump } from "../cockpit/sectionJump";
import { collabExtensions } from "./schema";
import { sectionHeadingDom, useSectionJump } from "./useSectionJump";

// jsdom does not implement scrollIntoView; the hook's whole job is calling it.
const scrolled: HTMLElement[] = [];
beforeEach(() => {
  scrolled.length = 0;
  resetSectionJump();
  Element.prototype.scrollIntoView = function (this: HTMLElement) {
    scrolled.push(this);
  } as unknown as typeof Element.prototype.scrollIntoView;
});
afterEach(() => {
  resetSectionJump();
});

const DOC =
  "<p>intro</p><h2>Background</h2><p>b</p><h2>Methods</h2><p>m</p><h2>Budget</h2><p>x</p>";

function mk(content = DOC) {
  return new Editor({ extensions: collabExtensions, content });
}

describe("sectionHeadingDom — the sectionizer's index, resolved in the document", () => {
  it("maps section N to the Nth H2, in document order", () => {
    const e = mk();
    expect(sectionHeadingDom(e, 1)?.textContent).toBe("Background");
    expect(sectionHeadingDom(e, 2)?.textContent).toBe("Methods");
    expect(sectionHeadingDom(e, 3)?.textContent).toBe("Budget");
  });

  it("resolves section 0 (the preamble) to the top of the document", () => {
    const e = mk();
    // The preamble has no heading node of its own; sectionizer.py guarantees
    // section 0 exists whether or not it holds any text.
    expect(sectionHeadingDom(e, 0)).toBe(e.view.dom);
  });

  it("does NOT count H3 as a section boundary", () => {
    // sectionizer.py splits on `##` and explicitly not `###` (_H2_RE uses a
    // negative lookahead). A toolbar-inserted H3 must not shift every index.
    const e = mk("<h2>One</h2><h3>Not a section</h3><p>t</p><h2>Two</h2>");
    expect(sectionHeadingDom(e, 2)?.textContent).toBe("Two");
  });

  it("returns null when the document does not hold that section yet", () => {
    // The empty-Yjs-doc case: the editor mounts before the provider syncs.
    expect(sectionHeadingDom(mk("<p></p>"), 2)).toBeNull();
    expect(sectionHeadingDom(mk(DOC), 9)).toBeNull();
  });
});

function Harness({ editor }: { editor: Editor | null }) {
  useSectionJump(editor);
  return null;
}

describe("useSectionJump", () => {
  it("scrolls to the section the Outline asks for", () => {
    const e = mk();
    render(<Harness editor={e} />);
    requestSectionJump(2);
    expect(scrolled.map((el) => el.textContent)).toEqual(["Methods"]);
  });

  it("consumes a request made BEFORE the editor mounted (tab switch)", () => {
    // Clicking the Outline on the scorecard tab switches tab first, so the
    // request is published while no artifact editor exists.
    requestSectionJump(3);
    render(<Harness editor={mk()} />);
    expect(scrolled.map((el) => el.textContent)).toEqual(["Budget"]);
  });

  it("retries once the document syncs instead of giving up", () => {
    const e = mk("<p></p>"); // empty, as a fresh Yjs doc is
    render(<Harness editor={e} />);
    requestSectionJump(2);
    expect(scrolled).toEqual([]); // nothing to scroll to yet — and it did NOT throw
    e.commands.setContent(DOC); // the provider syncs
    expect(scrolled.map((el) => el.textContent)).toEqual(["Methods"]);
  });

  it("scrolls once per request, not on every keystroke", () => {
    const e = mk();
    render(<Harness editor={e} />);
    requestSectionJump(2);
    e.commands.insertContent("typing");
    e.commands.insertContent(" more");
    expect(scrolled).toHaveLength(1);
  });

  it("honours a repeat request for the section already in the URL", () => {
    // The reason this is an event and not URL state: ?sec never changed here.
    const e = mk();
    render(<Harness editor={e} />);
    requestSectionJump(2);
    requestSectionJump(2);
    expect(scrolled.map((el) => el.textContent)).toEqual(["Methods", "Methods"]);
  });
});
