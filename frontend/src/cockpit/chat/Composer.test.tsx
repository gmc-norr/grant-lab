import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Composer } from "./Composer";

const h = vi.hoisted(() => ({
  submit: vi.fn(),
  upload: vi.fn(),
  uploadPending: false,
}));
vi.mock("../../api/turns", () => ({
  useSubmitTurn: () => ({ mutate: h.submit, isPending: false }),
}));
vi.mock("../../api/files", () => ({
  useUploadFile: () => ({ mutate: h.upload, isPending: h.uploadPending }),
}));

beforeEach(() => {
  h.submit.mockReset();
  h.upload.mockReset();
  h.uploadPending = false;
});
afterEach(() => { vi.restoreAllMocks(); });

function wrap(ui: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

describe("Composer image attachments", () => {
  it("uploads a picked image and includes it in the turn", async () => {
    h.upload.mockImplementation((_file, opts) =>
      opts.onSuccess({ attachment_id: "a1", filename: "fig.png", mime_type: "image/png" }),
    );
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    const input = screen.getByLabelText(/attach image/i) as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File([new Uint8Array([1])], "fig.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(screen.getByText("fig.png")).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "see fig" } });
    fireEvent.click(screen.getByRole("button", { name: /send/i }));
    expect(h.submit).toHaveBeenCalledWith(
      expect.objectContaining({ content: "see fig", attachment_ids: ["a1"] }),
      expect.anything(),
    );
  });

  it("removes an attached image chip", async () => {
    h.upload.mockImplementation((_file, opts) =>
      opts.onSuccess({ attachment_id: "a1", filename: "fig.png", mime_type: "image/png" }),
    );
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/attach image/i), {
      target: { files: [new File([new Uint8Array([1])], "fig.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(screen.getByText("fig.png")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /remove fig\.png/i }));
    expect(screen.queryByText("fig.png")).not.toBeInTheDocument();
  });

  it("disables Send while an image upload is in flight", () => {
    // Upload started, not yet resolved: isPending=true, onSuccess hasn't fired,
    // so `images` is still empty. Sending now would post attachment_ids:[] and
    // silently drop the image — the Send guard must block until it settles.
    h.uploadPending = true;
    wrap(<Composer id="ws1" refs={[]} disabled={false} onSubmitted={vi.fn()} />);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "hi" } });
    expect(screen.getByRole("button", { name: /send/i })).toBeDisabled();
  });
});
