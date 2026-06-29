import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

import { TurnList } from "./TurnList";

const refs = [{ id: "r1", citekey: "smith2021", title: "Smith 2021", kind: "paper", text_status: "done" }];

it("renders user text, assistant text, a compact tool line, and a reference chip", () => {
  render(
    <TurnList
      id="ws1"
      refs={refs as never}
      turns={[
        { id: "1", role: "user", position: 1, created_at: "", content: [{ type: "text", text: "Use this" }, { type: "reference", reference_id: "r1" }] },
        { id: "2", role: "tool", position: 2, created_at: "", content: { type: "tool_use", tool_name: "read_proposal" } },
        { id: "3", role: "assistant", position: 3, created_at: "", content: { type: "text", text: "Proposed an edit." } },
        { id: "4", role: "assistant", position: 4, created_at: "", content: { type: "turn_outcome", text: "Proposed edit → Methods" } },
      ] as never}
    />,
  );
  expect(screen.getByText("Use this")).toBeInTheDocument();
  expect(screen.getByText(/smith2021/)).toBeInTheDocument();   // reference chip resolved from refs
  expect(screen.getByText(/read_proposal/)).toBeInTheDocument(); // compact tool line
  expect(screen.getByText("Proposed an edit.")).toBeInTheDocument();
  expect(screen.getByText(/Proposed edit → Methods/)).toBeInTheDocument(); // outcome chip
});

it("falls back to a muted chip for an unknown reference id", () => {
  render(
    <TurnList
      id="ws1"
      refs={[] as never}
      turns={[{ id: "1", role: "user", position: 1, created_at: "", content: [{ type: "reference", reference_id: "gone" }] }] as never}
    />,
  );
  expect(screen.getByText(/reference/i)).toBeInTheDocument();
});

it("renders a selection block as a quoted chip, not a raw tag", () => {
  render(
    <TurnList
      id="ws1"
      refs={[] as never}
      turns={[
        { id: "t1", role: "user", position: 1, created_at: "", content: [
          { type: "text", text: "Shorten this." },
          { type: "selection", request_type: "modify", section_index: 2, verified: true,
            anchor: "We will do the work.", from_offset: 0, to_offset: 20 },
        ] },
      ] as never}
    />,
  );
  expect(screen.getByText("Shorten this.")).toBeInTheDocument();
  expect(screen.getByText(/We will do the work\./)).toBeInTheDocument();
  expect(screen.queryByText(/<selection/)).toBeNull();
  // stronger: no raw <selection> element exists in the DOM either
  expect(document.querySelector("selection")).toBeNull();
});

it("renders a selection block with 'unsaved' marker when not verified", () => {
  render(
    <TurnList
      id="ws1"
      refs={[] as never}
      turns={[
        { id: "t2", role: "user", position: 1, created_at: "", content: [
          { type: "selection", request_type: "ask", section_index: 0, verified: false,
            anchor: "Some text.", from_offset: 0, to_offset: 10 },
        ] },
      ] as never}
    />,
  );
  expect(screen.getByText(/Some text\./)).toBeInTheDocument();
  expect(screen.getByText(/unsaved/)).toBeInTheDocument();
});

it("renders an image block as an img pointing at the files serve route", () => {
  render(
    <TurnList
      id="ws1"
      turns={
        [
          {
            id: "t1",
            role: "user",
            position: 1,
            created_at: "x",
            content: [
              { type: "text", text: "see this" },
              { type: "image", attachment_id: "a1" },
            ],
          },
        ] as never
      }
      refs={[] as never}
    />,
  );
  const img = screen.getByRole("img") as HTMLImageElement;
  expect(img.getAttribute("src")).toBe("/api/workspaces/ws1/files/a1");
});
