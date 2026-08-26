import { Editor } from "@tiptap/core";
import { MantineProvider } from "@mantine/core";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ReactNode } from "react";

import { EditorToolbar } from "./EditorToolbar";
import { collabExtensions } from "./schema";

function mk(content = "<p>word</p>") {
  const e = new Editor({ extensions: collabExtensions, content });
  e.commands.setTextSelection({ from: 1, to: 5 });
  return e;
}

function mount(editor: Editor, canWrite = true, actions?: ReactNode) {
  return render(
    <MantineProvider>
      <EditorToolbar editor={editor} canWrite={canWrite} actions={actions} />
    </MantineProvider>
  );
}

describe("EditorToolbar — commands exist", () => {
  it("every command the toolbar calls is present on this editor", () => {
    // The load-bearing guard. The schema is hand-rolled, so StarterKit's
    // convenience commands are absent; if someone swaps a call for
    // toggleBulletList() this fails loudly instead of rendering a dead button.
    const e = mk();
    for (const name of [
      "setNode", "toggleMark", "toggleList", "toggleWrap",
      "setMark", "unsetMark", "insertContent",
    ]) {
      expect(typeof (e.commands as Record<string, unknown>)[name]).toBe("function");
    }
  });
});

describe("EditorToolbar — marks", () => {
  it("bold applies strong", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-strong"));
    expect(e.getHTML()).toContain("<strong>word</strong>");
  });
  it("italic applies em", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-em"));
    expect(e.getHTML()).toContain("<em>word</em>");
  });
  it("strikethrough applies s", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-strikethrough"));
    expect(e.getHTML()).toContain("<s>word</s>");
  });
  it("inline code applies code", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-code"));
    expect(e.getHTML()).toContain("<code>word</code>");
  });
});

describe("EditorToolbar — blocks", () => {
  it("bullet list wraps in ul/li", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-bullet_list"));
    expect(e.getHTML()).toContain("<ul><li><p>word</p></li></ul>");
  });
  it("numbered list wraps in ol/li", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-ordered_list"));
    // NOTE: the schema spreads attrs into the tag, so this is <ol order="1">,
    // never a bare <ol>. Asserting "<ol>" fails against correct code.
    expect(e.getHTML()).toContain('<ol order="1">');
    expect(e.getHTML()).toContain("<li><p>word</p></li>");
  });
  it("blockquote wraps the paragraph", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-blockquote"));
    expect(e.getHTML()).toContain("<blockquote><p>word</p></blockquote>");
  });
  it("horizontal rule inserts an hr", () => {
    const e = mk();
    mount(e);
    fireEvent.click(screen.getByTestId("tb-horizontal_rule"));
    expect(e.getHTML()).toContain("<hr>");
  });
});

describe("EditorToolbar — block type dropdown", () => {
  it("switching to Heading 3 converts the paragraph", () => {
    const e = mk();
    mount(e);
    fireEvent.change(screen.getByTestId("tb-blocktype"), { target: { value: "h3" } });
    // Attr leaks into the tag — assert the opening tag, not "<h3>".
    expect(e.getHTML()).toContain("<h3");
    expect(e.getHTML()).toContain("word");
  });
  it("switching to Body converts a heading back to a paragraph", () => {
    const e = mk("<h3>word</h3>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    mount(e);
    fireEvent.change(screen.getByTestId("tb-blocktype"), { target: { value: "p" } });
    expect(e.getHTML()).toContain("<p>word</p>");
  });
  it("reflects the block the cursor is in", () => {
    const e = mk("<h2>word</h2>");
    e.commands.setTextSelection({ from: 1, to: 5 });
    mount(e);
    expect((screen.getByTestId("tb-blocktype") as HTMLSelectElement).value).toBe("h2");
  });
});

describe("EditorToolbar — block type is gated on can()", () => {
  it("DESTROYS NOTHING in a list item: the heading options are disabled", () => {
    // Measured: inside a list item, editor.can().setNode("heading") is false but
    // commands.setNode("heading") returns TRUE and lifts the text out of the
    // list, silently destroying the <ul><li> wrapper. The gate is the only
    // thing standing between the user and that data loss.
    const e = new Editor({ extensions: collabExtensions, content: "<ul><li><p>item</p></li></ul>" });
    e.commands.setTextSelection(4);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    const h2 = Array.from(select.options).find((o) => o.value === "h2")!;
    expect(h2.disabled).toBe(true);
    // And if it were somehow chosen, the list must survive.
    fireEvent.change(select, { target: { value: "h2" } });
    expect(e.getHTML()).toContain("<ul>");
    expect(e.getHTML()).toContain("<li>");
  });

  it("disables heading options inside a table cell", () => {
    const e = new Editor({
      extensions: collabExtensions,
      content: "<table><tr><td><p>cell</p></td></tr></table>",
    });
    e.commands.setTextSelection(4);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(true);
  });

  it("leaves heading options ENABLED in an ordinary paragraph", () => {
    // Guard against 'fix' by disabling everything everywhere.
    const e = mk();
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(false);
  });

  it("leaves heading options ENABLED inside a blockquote", () => {
    // Measured: a heading IS valid inside a blockquote; can() returns true.
    const e = new Editor({
      extensions: collabExtensions,
      content: "<blockquote><p>quoted</p></blockquote>",
    });
    e.commands.setTextSelection(3);
    mount(e);
    const select = screen.getByTestId("tb-blocktype") as HTMLSelectElement;
    expect(Array.from(select.options).find((o) => o.value === "h2")!.disabled).toBe(false);
  });
});

describe("EditorToolbar — permissions and slots", () => {
  it("every control is disabled when canWrite is false", () => {
    const e = mk();
    mount(e, false);
    for (const id of [
      "tb-blocktype", "tb-strong", "tb-em", "tb-strikethrough", "tb-code",
      "tb-bullet_list", "tb-ordered_list", "tb-blockquote", "tb-horizontal_rule",
    ]) {
      expect(screen.getByTestId(id)).toBeDisabled();
    }
  });
  it("renders the actions slot", () => {
    const e = mk();
    mount(e, true, <button data-testid="panel-action">Export</button>);
    expect(screen.getByTestId("panel-action")).toBeInTheDocument();
  });
  it("renders nothing without an editor", () => {
    // Render without MantineProvider so the container stays empty — the
    // provider injects <style> tags that would make toBeEmptyDOMElement fail
    // even though the toolbar itself returns null.
    const { container } = render(<EditorToolbar editor={null} canWrite />);
    expect(container).toBeEmptyDOMElement();
  });
});
