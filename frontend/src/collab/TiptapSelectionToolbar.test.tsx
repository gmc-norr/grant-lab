// frontend/src/collab/TiptapSelectionToolbar.test.tsx
import { MantineProvider } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { theme } from "../theme/theme";
import { deriveClaimAnchor } from "./selectionAnchor"; // vi-mocked below
import type { ClaimAnchor } from "./selectionAnchor";

// ---------------------------------------------------------------------------
// Mock selectionAnchor so tests don't need a real ProseMirror state
// ---------------------------------------------------------------------------
const fixedClaim: ClaimAnchor = {
  sectionIndex: 0,
  anchor: "Test claim.",
  singleSection: true,
  headingOnly: false,
};

vi.mock("./selectionAnchor", () => ({
  deriveClaimAnchor: vi.fn(() => fixedClaim),
}));

// Controllable submit/ack mocks — hoisted so the vi.mock factory can reference them.
// Tests drive the mutate options (onSuccess/onError) to exercise disclosure + errors.
const { submitMock, ackMock } = vi.hoisted(() => ({ submitMock: vi.fn(), ackMock: vi.fn() }));
vi.mock("../api/turns", () => ({
  useSubmitTurn: () => ({ mutate: submitMock, isPending: false }),
  useAckDisclosure: () => ({ mutate: ackMock, isPending: false }),
}));

// Mock CitationComposer to avoid its own API hooks in gate tests
vi.mock("../cockpit/CitationComposer", () => ({
  CitationComposer: () => <div data-testid="citation-composer" />,
}));

import { TiptapSelectionToolbar } from "./TiptapSelectionToolbar";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A minimal fake editor that satisfies the toolbar's useEffect listener API.
 *  `fire(event)` invokes the registered listeners so a test can drive selectionUpdate. */
function fakeEditor() {
  const listeners: Map<string, (() => void)[]> = new Map();
  return {
    state: { selection: {} } as never,
    on: vi.fn((event: string, cb: () => void) => {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event)!.push(cb);
    }),
    off: vi.fn(),
    fire: (event: string) => listeners.get(event)?.forEach((fn) => fn()),
  } as unknown as Editor & { fire: (event: string) => void };
}

/** A minimal fake provider */
function fakeProvider(connected: boolean, settled: boolean) {
  return {
    get status() {
      return connected ? "connected" : "connecting";
    },
    isSettled: () => settled,
  } as never;
}

const SECTIONS = [{ sha: "s1sha", text: "Test claim." }];
const HEAD = "a".repeat(40);

function renderToolbar(overrides: {
  canEdit?: boolean;
  canWrite?: boolean;
  connected?: boolean;
  settled?: boolean;
  sections?: { sha: string; text: string }[];
} = {}) {
  const {
    canEdit = true,
    canWrite = true,
    connected = true,
    settled = true,
    sections = SECTIONS,
  } = overrides;
  const qc = new QueryClient();
  render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <TiptapSelectionToolbar
          editor={fakeEditor()}
          provider={fakeProvider(connected, settled)}
          workspaceId="ws-1"
          head={HEAD}
          sections={sections}
          canEdit={canEdit}
          canWrite={canWrite}
        />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("TiptapSelectionToolbar gates", () => {
  it("owner-writer settled: Ask + Edit + Cite all visible and enabled", () => {
    renderToolbar({ canEdit: true, canWrite: true, settled: true });
    expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /edit/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /edit/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /cite/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cite/i })).toBeEnabled();
  });

  it("canEdit:false → Edit button not rendered; Ask and Cite still present", () => {
    renderToolbar({ canEdit: false, canWrite: true, settled: true });
    expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.getByRole("button", { name: /cite/i })).toBeInTheDocument();
  });

  it("canWrite:false + canEdit:false → no Cite, no Edit; Ask still present", () => {
    renderToolbar({ canEdit: false, canWrite: false, settled: true });
    expect(screen.getByRole("button", { name: /ask/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^cite$/i })).toBeNull();
  });

  it("unsettled provider → Cite and Edit disabled (Ask still enabled)", () => {
    renderToolbar({ canEdit: true, canWrite: true, connected: true, settled: false });
    expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cite/i })).toBeDisabled();
  });

  it("non-unique anchor in section → all actions disabled (avoids wrong-target)", () => {
    // "Test claim." appears TWICE → indexOf would silently target the first
    // occurrence, not the one the user selected. Refuse rather than mis-target (C#1).
    renderToolbar({ sections: [{ sha: "s1sha", text: "Test claim. Test claim." }] });
    expect(screen.getByRole("button", { name: /ask/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /cite/i })).toBeDisabled();
  });

  it("reactive settle: Cite/Edit enable on the poll tick after the buffer drains", () => {
    vi.useFakeTimers();
    try {
      const ref = { val: false };
      const provider = {
        get status() {
          return "connected";
        },
        isSettled: () => ref.val, // controllable — false until we drain
      } as never;
      const qc = new QueryClient();
      render(
        <MantineProvider theme={theme} defaultColorScheme="dark">
          <QueryClientProvider client={qc}>
            <TiptapSelectionToolbar
              editor={fakeEditor()}
              provider={provider}
              workspaceId="ws-1"
              head={HEAD}
              sections={SECTIONS}
              canEdit={true}
              canWrite={true}
            />
          </QueryClientProvider>
        </MantineProvider>,
      );
      // Unsettled → Ask enabled (anchorable) but Cite/Edit disabled (the latch symptom).
      expect(screen.getByRole("button", { name: /ask/i })).toBeEnabled();
      expect(screen.getByRole("button", { name: /cite/i })).toBeDisabled();
      expect(screen.getByRole("button", { name: /edit/i })).toBeDisabled();
      // Buffer drains → the poll re-enables the write-actions (no reselect needed).
      ref.val = true;
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByRole("button", { name: /cite/i })).toBeEnabled();
      expect(screen.getByRole("button", { name: /edit/i })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("TiptapSelectionToolbar Ask errors", () => {
  beforeEach(() => {
    submitMock.mockReset();
    ackMock.mockReset();
    vi.mocked(deriveClaimAnchor).mockReturnValue(fixedClaim); // restore default claim
  });

  // Open the Ask menu and click the "Explain this" preset → runTurn("ask", ...).
  // The Mantine Menu dropdown renders in a portal after a transition, so the menuitem
  // must be awaited (findByRole), mirroring SelectionToolbar.test's pattern.
  async function clickAskExplain() {
    fireEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /explain this/i }));
  }

  it("disclosure 403 → inline ack banner → ack + retry re-submits", async () => {
    // First submit rejects with a disclosure error via the onError option.
    // ApiError(code: string, message: string); .message is `${code}: ${message}`.
    submitMock.mockImplementationOnce((_vars, opts) =>
      opts.onError(new ApiError("disclosure_required", "hosted-model disclosure not acknowledged")),
    );
    renderToolbar({ settled: true });
    await clickAskExplain();
    expect(await screen.findByRole("alert")).toHaveTextContent(/hosted model/i);

    // Clicking ack calls ack.mutate and, on its onSuccess, re-runs the held turn.
    ackMock.mockImplementationOnce((_v, opts) => opts.onSuccess());
    submitMock.mockImplementationOnce((_vars, opts) => opts.onSuccess?.());
    fireEvent.click(await screen.findByRole("button", { name: /i understand/i }));
    expect(ackMock).toHaveBeenCalled();
    expect(submitMock).toHaveBeenCalledTimes(2); // original + retry
  });

  it("non-disclosure error → generic alert, no ack banner", async () => {
    submitMock.mockImplementationOnce((_vars, opts) => {
      opts.onError(new ApiError("server_error", "boom"));
    });
    renderToolbar({ settled: true });
    await clickAskExplain();
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn.t do that/i);
    expect(screen.queryByText(/hosted model/i)).toBeNull();
  });

  it("ack-retry re-submits the FROZEN payload even if the selection changed (Codex High)", async () => {
    const editor = fakeEditor(); // exposes .fire so we can move the selection
    submitMock.mockImplementationOnce((_vars, opts) =>
      opts.onError(
        new ApiError("disclosure_required", "hosted-model disclosure not acknowledged"),
      ),
    );
    const qc = new QueryClient();
    render(
      <MantineProvider theme={theme} defaultColorScheme="dark">
        <QueryClientProvider client={qc}>
          <TiptapSelectionToolbar
            editor={editor}
            provider={fakeProvider(true, true)}
            workspaceId="ws-1"
            head={HEAD}
            sections={SECTIONS}
            canEdit
            canWrite
          />
        </QueryClientProvider>
      </MantineProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /^ask$/i }));
    fireEvent.click(await screen.findByRole("menuitem", { name: /explain this/i }));
    expect(submitMock.mock.calls[0][0].selection_context.anchor).toBe("Test claim.");
    expect(await screen.findByRole("alert")).toHaveTextContent(/hosted model/i);

    // User moves the selection to a DIFFERENT claim before acking.
    vi.mocked(deriveClaimAnchor).mockReturnValue({
      sectionIndex: 0,
      anchor: "Other claim.",
      singleSection: true,
      headingOnly: false,
    });
    act(() => editor.fire("selectionUpdate"));

    // Ack → the retry MUST re-send the ORIGINAL "Test claim." context, not "Other claim.".
    ackMock.mockImplementationOnce((_v, opts) => opts.onSuccess());
    submitMock.mockImplementationOnce((_vars, opts) => opts.onSuccess?.());
    fireEvent.click(await screen.findByRole("button", { name: /i understand/i }));
    expect(submitMock).toHaveBeenCalledTimes(2);
    expect(submitMock.mock.calls[1][0].selection_context.anchor).toBe("Test claim.");
  });
});
