// frontend/src/collab/TiptapSelectionToolbar.test.tsx
import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { theme } from "../theme/theme";
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

// Mock useSubmitTurn — the tests only verify gate visibility, not submission
vi.mock("../api/turns", () => ({
  useSubmitTurn: () => ({ mutate: vi.fn(), isPending: false }),
}));

// Mock CitationComposer to avoid its own API hooks in gate tests
vi.mock("../cockpit/CitationComposer", () => ({
  CitationComposer: () => <div data-testid="citation-composer" />,
}));

import { TiptapSelectionToolbar } from "./TiptapSelectionToolbar";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A minimal fake editor that satisfies the toolbar's useEffect listener API */
function fakeEditor() {
  const listeners: Map<string, (() => void)[]> = new Map();
  return {
    state: { selection: {} } as never,
    on: vi.fn((event: string, cb: () => void) => {
      if (!listeners.has(event)) listeners.set(event, []);
      listeners.get(event)!.push(cb);
    }),
    off: vi.fn(),
  } as never;
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
