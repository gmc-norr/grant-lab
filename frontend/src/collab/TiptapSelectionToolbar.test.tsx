// frontend/src/collab/TiptapSelectionToolbar.test.tsx
import { MantineProvider } from "@mantine/core";
import type { Editor } from "@tiptap/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode, Ref } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
const { bubbleMenuMock } = vi.hoisted(() => ({ bubbleMenuMock: vi.fn() }));
type BubbleMenuTestProps = {
  children?: ReactNode;
  options?: Record<string, unknown>;
  shouldShow?: (props: { state: unknown }) => boolean;
  ref?: Ref<HTMLDivElement>;
};
// The real BubbleMenu forwards its ref to the wrapper div the tiptap plugin positions;
// the mock does the same so the toolbar's ResizeObserver has an element to observe.
vi.mock("@tiptap/react/menus", () => ({
  BubbleMenu: (props: BubbleMenuTestProps) => {
    bubbleMenuMock(props);
    return <div ref={props.ref} data-testid="bubble-menu">{props.children}</div>;
  },
}));
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
  editor?: Editor;
} = {}) {
  const {
    canEdit = true,
    canWrite = true,
    connected = true,
    settled = true,
    sections = SECTIONS,
    editor = fakeEditor(),
  } = overrides;
  const qc = new QueryClient();
  return render(
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={qc}>
        <TiptapSelectionToolbar
          editor={editor}
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
  it("renders the actions inside a viewport-aware Tiptap bubble menu", () => {
    renderToolbar();

    const bubble = screen.getByTestId("bubble-menu");
    expect(bubble).toContainElement(screen.getByRole("toolbar", { name: "Selection actions" }));
    const props = bubbleMenuMock.mock.lastCall![0] as BubbleMenuTestProps;
    expect(props.options).toMatchObject({
      strategy: "fixed",
      placement: "top",
      offset: 8,
      flip: true,
      shift: { padding: 8 },
      inline: true,
    });
    expect(props.shouldShow?.({ state: {} })).toBe(true);
  });

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

describe("TiptapSelectionToolbar custom edits", () => {
  beforeEach(() => {
    submitMock.mockReset();
    ackMock.mockReset();
    vi.mocked(deriveClaimAnchor).mockReturnValue(fixedClaim);
  });

  it("submits a custom instruction as a modify turn for the selected text", async () => {
    renderToolbar({ settled: true });

    fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    const input = await screen.findByRole("textbox", { name: /custom edit instruction/i });
    fireEvent.change(input, { target: { value: "  Make this persuasive for a clinical audience.  " } });
    fireEvent.submit(input.closest("form")!);

    expect(submitMock).toHaveBeenCalledTimes(1);
    expect(submitMock.mock.calls[0][0]).toMatchObject({
      content: "Make this persuasive for a clinical audience.",
      selection_context: {
        request_type: "modify",
        target_artifact: "draft",
        section_index: 0,
        anchor: "Test claim.",
      },
    });
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

describe("TiptapSelectionToolbar repositioning", () => {
  // The tiptap bubble-menu plugin measures its wrapper the instant the selection changes —
  // BEFORE React commits the Paper into it. Measured in-browser 2026-09-03: the wrapper is
  // appended EMPTY (0 px), the Paper mounts ~50 ms later, and the plugin's position write
  // then lands one own-height too low — ON the selected text — for every single-shot
  // selection (dblclick, one-step drag, synthetic range). Nothing re-measures on its own.
  // The plugin's one reposition hook is a transaction carrying
  // setMeta(pluginKey, "updatePosition"); this pins the wiring wrapper → resize → that
  // transaction. The geometry itself is pinned by e2e/collab.spec.ts (gap <= 24 px).
  type RoCallback = (entries: unknown[], observer: unknown) => void;
  type Captured = { cb: RoCallback; observe: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> };
  let captured: Captured[];
  let realResizeObserver: typeof ResizeObserver;

  beforeEach(() => {
    captured = [];
    realResizeObserver = globalThis.ResizeObserver;
    // Mantine may construct observers of its own, so each instance is captured and the
    // test picks the one that observed the bubble-menu wrapper.
    globalThis.ResizeObserver = class {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
      constructor(cb: RoCallback) {
        captured.push({ cb, observe: this.observe, disconnect: this.disconnect });
      }
    } as unknown as typeof ResizeObserver;
  });
  afterEach(() => {
    globalThis.ResizeObserver = realResizeObserver;
  });

  /** fakeEditor() plus the view/state surface the reposition dispatch needs. */
  function editorWithView() {
    const editor = fakeEditor();
    const tr = { setMeta: vi.fn() };
    tr.setMeta.mockReturnValue(tr);
    const dispatch = vi.fn();
    Object.assign(editor, {
      isDestroyed: false,
      state: { selection: {}, tr },
      view: { dispatch },
    });
    return { editor, tr, dispatch };
  }

  const grown = [{ contentRect: { height: 52 } }]; // what the wrapper reports once the Paper is in it
  const hidden = [{ contentRect: { height: 0 } }]; // what it reports after the plugin's hide() detaches it

  function wrapperObserver(): Captured {
    const wrapper = screen.getByTestId("bubble-menu");
    const ro = captured.find((c) => c.observe.mock.calls.some((call) => call[0] === wrapper));
    expect(ro, "no ResizeObserver observed the bubble-menu wrapper").toBeDefined();
    return ro!;
  }

  it("observes the bubble-menu wrapper and asks the plugin to re-position when it resizes", () => {
    const { editor, tr, dispatch } = editorWithView();
    const { unmount } = renderToolbar({ editor });
    const ro = wrapperObserver();
    expect(dispatch).not.toHaveBeenCalled();

    act(() => ro.cb(grown, {}));

    expect(tr.setMeta).toHaveBeenCalledWith("draft-selection-actions", "updatePosition");
    expect(dispatch).toHaveBeenCalledTimes(1);
    expect(dispatch).toHaveBeenCalledWith(tr);

    unmount();
    expect(ro.disconnect).toHaveBeenCalled();
  });

  it("does not dispatch into a destroyed editor", () => {
    const { editor, dispatch } = editorWithView();
    renderToolbar({ editor });
    const ro = wrapperObserver();
    (editor as unknown as { isDestroyed: boolean }).isDestroyed = true;

    act(() => ro.cb(grown, {}));

    expect(dispatch).not.toHaveBeenCalled();
  });

  it("ignores the zero-height notification a hidden (detached) wrapper produces", () => {
    const { editor, dispatch } = editorWithView();
    renderToolbar({ editor });
    const ro = wrapperObserver();

    act(() => ro.cb(hidden, {}));

    expect(dispatch).not.toHaveBeenCalled();
  });
});
