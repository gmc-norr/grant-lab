// frontend/src/cockpit/SelectionToolbar.test.tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import * as clientMod from "../api/client";
import { theme } from "../theme/theme";
import { SelectionToolbar } from "./SelectionToolbar";

afterEach(() => { vi.restoreAllMocks(); });

function renderTB(over = {}) {
  const qc = new QueryClient();
  const props = {
    workspaceId: "ws-1",
    selection: { from: 0, to: 5, text: "hello" },
    editorValue: "hello world",
    sectionIndex: 1, headSha: "a".repeat(40), sectionSha: "s1",
    canEdit: true, isDirty: false, onCite: vi.fn(), onSubmitted: vi.fn(),
    ...over,
  };
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}><SelectionToolbar {...props} /></QueryClientProvider>
    </MantineProvider>,
  );
  return props;
}

it("renders nothing without a selection", () => {
  const { container } = render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={new QueryClient()}>
        <SelectionToolbar workspaceId="ws-1" selection={null} editorValue="" sectionIndex={0}
          headSha="" sectionSha="" canEdit isDirty={false} onCite={() => {}} />
      </QueryClientProvider>
    </MantineProvider>,
  );
  expect(container.querySelector("button")).toBeNull();
});

it("Ask stays enabled when dirty; Edit/Cite disabled when dirty", () => {
  renderTB({ isDirty: true });
  expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
  expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
  const cite = screen.getByRole("button", { name: /cite/i });
  expect(cite).toBeDisabled();
  expect(cite).toHaveAttribute("title", "Save section to cite");
});

it("non-writer sees Ask only", () => {
  renderTB({ canEdit: false });
  expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^edit/i })).toBeNull();
  expect(screen.queryByRole("button", { name: /^cite/i })).toBeNull();
});

it("Edit preset submits a modify turn with code-point offsets", async () => {
  const post = vi.spyOn(clientMod.api, "POST").mockResolvedValue({ data: { job_id: "j" }, error: undefined } as never);
  renderTB({ selection: { from: 6, to: 11, text: "world" }, editorValue: "hello world" });
  fireEvent.click(screen.getByRole("button", { name: /edit/i }));
  fireEvent.click(await screen.findByRole("menuitem", { name: /shorten/i }));
  await waitFor(() => expect(post).toHaveBeenCalled());
  const body = (post.mock.calls[0][1] as { body: Record<string, any> }).body;
  expect(body.selection_context).toMatchObject({
    request_type: "modify", section_index: 1, from_offset: 6, to_offset: 11, anchor: "world",
  });
});

it("Cite delegates to onCite (no turn)", () => {
  const post = vi.spyOn(clientMod.api, "POST");
  const props = renderTB();
  fireEvent.click(screen.getByRole("button", { name: /cite/i }));
  expect(props.onCite).toHaveBeenCalled();
  expect(post).not.toHaveBeenCalled();
});

it("free-text Edit is disabled when dirty; free-text Ask remains enabled", async () => {
  renderTB({ canEdit: true, isDirty: true });
  fireEvent.change(screen.getByLabelText(/selection instruction/i), { target: { value: "Improve flow" } });
  // bottom-row free-text Edit button is the last "edit"-labelled button (top Menu.Target is first)
  const editBtns = screen.getAllByRole("button", { name: /^edit$/i });
  expect(editBtns.at(-1)).toBeDisabled();
  // bottom-row free-text Ask button must still be enabled while dirty
  const askBtns = screen.getAllByRole("button", { name: /^ask$/i });
  expect(askBtns.at(-1)).toBeEnabled();
});

it("member: a disclosure 403 shows ack-and-retry, then submits on ack", async () => {
  const post = vi.spyOn(clientMod.api, "POST")
    // 1) the ask -> structured disclosure 403 (unwrap throws ApiError)
    .mockResolvedValueOnce({
      data: undefined,
      error: { error: { code: "disclosure_required", message: "Please acknowledge the data-disclosure notice before asking." } },
      response: { status: 403, ok: false } as Response,
    } as never)
    .mockResolvedValueOnce({ data: { acknowledged: true }, error: undefined } as never)  // 2) ack
    .mockResolvedValueOnce({ data: { job_id: "j" }, error: undefined } as never);          // 3) retry
  renderTB({ canEdit: false }); // a member sees Ask only
  fireEvent.change(screen.getByLabelText(/selection instruction/i), { target: { value: "Explain" } });
  // two "Ask" controls exist (menu target + free-text submit); the free-text one is last
  fireEvent.click(screen.getAllByRole("button", { name: /^ask$/i }).at(-1)!);
  fireEvent.click(await screen.findByRole("button", { name: /i understand/i }));
  await waitFor(() => expect(post).toHaveBeenCalledTimes(3)); // ask -> ack -> retry
  const lastBody = (post.mock.calls.at(-1)![1] as { body: Record<string, any> }).body;
  expect(lastBody.selection_context?.request_type).toBe("ask");
});
